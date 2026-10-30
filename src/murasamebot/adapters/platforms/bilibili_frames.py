"""Player-preview sampling; no playurl, browser, FFmpeg or video download.

Interface/design references: docs/VIDEO_VISION.md. This Python implementation
keeps time-to-tile mapping explicit, including the sheet-local row offset.
"""
from __future__ import annotations

from collections import OrderedDict
from contextlib import closing
from dataclasses import replace
import math
import struct
from threading import RLock
import time
from urllib.parse import urlparse

import httpx
from murasamebot.adapters.network import client as network_client

from murasamebot.domain.models import FrameContext, ImageRef, PlatformError, stable_id


def fetch_preview_index(url: str, *, proxy_url="", client=None) -> list[int]:
    """Cookie-free, bounded CDN read of the documented big-endian uint16 table."""
    if not isinstance(url, str):
        return []
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("http://"):
        url = "https://" + url[7:]
    try:
        parsed = urlparse(url)
        if (len(url) > 8192 or parsed.scheme != "https" or parsed.hostname not in
                {"i0.hdslb.com", "i1.hdslb.com", "i2.hdslb.com", "bimp.hdslb.com", "boss.hdslb.com"} or
                parsed.username or parsed.password or parsed.port not in {None, 443}):
            return []
    except ValueError:
        return []
    owned = client is None
    reader = client or network_client(proxy_url=proxy_url, timeout=15)
    try:
        # A fresh request omits client cookies even if a test/injected client
        # happens to hold cookies. Redirects never leave the allowlist.
        request = reader.build_request("GET", url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"})
        request.headers.pop("cookie", None)
        request.headers.pop("authorization", None)
        with closing(reader.send(request, stream=True, follow_redirects=False)) as response:
            if response.status_code in {403, 412, 429}:
                raise PlatformError("预览时间表CDN限制访问", code="read_limited" if response.status_code == 429 else "risk_control",
                                    pause=response.status_code != 429)
            if response.status_code != 200:
                return []
            data = bytearray()
            for chunk in response.iter_bytes(chunk_size=4096):
                data.extend(chunk)
                if len(data) > 20002:
                    return []
        if len(data) < 4 or len(data) % 2:
            return []
        values = list(struct.unpack(f">{len(data)//2}H", data))
        if values[0] != 0 or any(a > b for a, b in zip(values, values[1:])):
            return []
        # Header is not a tile, even if the first real timestamp is nonzero.
        return values[1:]
    except httpx.HTTPError:
        return []
    finally:
        if owned:
            reader.close()


def frame_policy(count: int, parts: int) -> tuple[int, int]:
    return max(1, min(count, 9)), max(1, min(parts, 3))


def sample_shot(data: dict, count: int, duration: float, part: int) -> tuple[ImageRef, ...]:
    """Pick nearest preview tiles to evenly spaced bin centers, not cover art."""
    dimensions = [data.get(k) for k in ("img_x_len", "img_y_len", "img_x_size", "img_y_size")]
    if any(type(v) is not int or v < 1 or v > 8192 for v in dimensions):
        return ()
    cols, rows, width, height = dimensions
    if cols * width > 8192 or rows * height > 8192:
        return ()
    sheets, times = data.get("image"), data.get("index")
    if not isinstance(sheets, list) or not isinstance(times, list):
        return ()
    # The documented pvdata JSON form has a leading zero header followed by
    # the first frame's zero timestamp. Also accept header-free timestamps.
    if len(times) > 1 and times[0] == times[1] == 0:
        times = times[1:]
    candidates, seen = [], set()
    for cell, timestamp in enumerate(times[:10000]):
        if type(timestamp) not in {int, float} or not math.isfinite(timestamp) or timestamp < 0:
            continue
        if timestamp in seen or (duration > 0 and timestamp > duration):
            continue
        sheet, local = divmod(cell, cols * rows)
        if sheet >= min(len(sheets), 100):
            break
        url = sheets[sheet]
        if not isinstance(url, str):
            continue
        if url.startswith("//"):
            url = "https:" + url
        elif url.startswith("http://"):
            url = "https://" + url[7:]
        try:
            parsed = urlparse(url)
            if (parsed.scheme != "https" or not (parsed.hostname or "").endswith(".hdslb.com") or
                    parsed.username or parsed.password or parsed.port not in {None, 443}):
                continue
        except ValueError:
            continue
        x, y = local % cols * width, local // cols * height
        candidates.append(ImageRef(url, 0, (x, y, width, height), float(timestamp), part))
        seen.add(timestamp)
    if not candidates:
        return ()
    candidates.sort(key=lambda image: image.timestamp_seconds)
    wanted = min(count, len(candidates))
    end = duration if duration > 0 else candidates[-1].timestamp_seconds
    picked = []
    available = list(candidates)
    for n in range(wanted):
        target = end * (n + .5) / wanted
        image = min(available, key=lambda item: abs(item.timestamp_seconds - target))
        available.remove(image)
        picked.append(image)
    return tuple(sorted(picked, key=lambda image: image.timestamp_seconds))


class VideoShotReader:
    def __init__(self, request, *, count=6, max_parts=3, cache_minutes=1440, retry_minutes=10, clock=time.time, index_reader=None):
        self.request, self.clock = request, clock
        self.count, self.max_parts = frame_policy(count, max_parts)
        self.cache_seconds, self.retry_seconds = cache_minutes * 60, retry_minutes * 60
        self._cache = OrderedDict()
        self._lock = RLock()
        self.index_reader = index_reader

    def read(self, video: dict) -> tuple[tuple[ImageRef, ...], FrameContext]:
        now = self.clock()
        bvid = video.get("bvid")
        pages = video.get("pages") or [{"cid": video.get("cid"), "duration": video.get("duration")}]
        if not isinstance(pages, list):
            pages = []
        aid = str(video.get("aid") or "")
        key = stable_id(bvid, aid, pages[:3], self.count, self.max_parts)
        with self._lock:
            old = self._cache.get(key)
            if old and now < old[0]:
                self._cache.move_to_end(key)
                return old[1]
        # Never hold the cache lock across network I/O: one slow video's
        # optional previews must not serialize unrelated conversations.
        result = self._read(bvid, pages, now, aid=aid)
        context = result[1]
        until = context.retry_after if context.status != "complete" else now + self.cache_seconds
        with self._lock:
            self._cache[key] = until, result
            while len(self._cache) > 64:
                self._cache.popitem(last=False)
            return result

    def _read(self, bvid, pages, now, *, aid=""):
        images, limits, sampled = [], [], []
        size = min(len(pages), self.max_parts, self.count)
        if len(pages) > size:
            limits.append(f"共{len(pages)}个分P，仅采样前{size}个")
        for offset, page in enumerate(pages[:size]):
            part = offset + 1
            cid = str(page.get("cid") or "") if isinstance(page, dict) else ""
            if not isinstance(bvid, str) or not bvid or not cid.isdigit():
                limits.append(f"分P{part}缺少有效快照标识")
                continue
            quota = self.count // size + (offset < self.count % size)
            duration = page.get("duration", 0)
            if type(duration) not in {int, float} or not math.isfinite(duration) or duration < 0:
                duration = 0
            attempts = [("/x/player/videoshot", {"bvid": bvid, "cid": cid, "index": 1}, "BV快照")]
            if aid.isdigit():
                attempts.append(("/x/player/videoshot", {"aid": aid, "cid": cid, "index": 1}, "AV快照"))
                if part == 1:
                    attempts.append(("/pvideo", {"aid": aid}, "首P预览接口"))
            frames, issues = (), []
            binary_seen = set()
            for path, params, label in attempts:
                try:
                    shot, _ = self.request(path, params=params)
                    if not isinstance(shot, dict):
                        issues.append(label + "响应格式无效")
                        continue
                    frames = sample_shot(shot, quota, duration, part)
                    if not frames and self.index_reader and shot.get("image") and shot.get("pvdata"):
                        url = shot["pvdata"]
                        if isinstance(url, str) and url not in binary_seen:
                            binary_seen.add(url)
                            times = self.index_reader(url)
                            # Add exactly one header here for sample_shot's
                            # existing documented double-zero normalization.
                            frames = sample_shot({**shot, "index": [0] + times if times and times[0] == 0 else times}, quota, duration, part)
                    if frames:
                        break
                    issues.append(label + ("有拼图但时间表缺失或无效" if shot.get("image") else "未返回拼图"))
                except PlatformError as exc:
                    if exc.pause or exc.retry_at or exc.code in {"-509", "read_limited", "read_deferred"}:
                        raise
                    issues.append(label + "不可用(" + str(exc.code)[:32] + ")")
            if frames:
                sampled.append(part)
                images.extend(frames)
            if len(frames) < quota:
                limits.append(f"分P{part}计划{quota}帧，取得{len(frames)}帧；" + "；".join(issues))
        status = "complete" if len(images) == self.count and not limits else "partial" if images else "unavailable"
        limits.append("仅为低清、离散时间点的播放器预览截图，未完整观看，不包含声音或连续动作")
        # Deliberately omitted later parts are a coverage limit, not a failed
        # acquisition to repeat every ten minutes.
        retry = self.retry_seconds if len(images) < self.count else self.cache_seconds
        context = FrameContext(status, self.count, self.max_parts, len(pages), tuple(sampled), tuple(limits),
                               now, now + retry)
        return tuple(replace(image, index=i + 1) for i, image in enumerate(images)), context
