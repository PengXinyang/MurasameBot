"""Player-preview sampling; no playurl, browser, FFmpeg or video download.

Interface/design references: docs/VIDEO_VISION.md. This Python implementation
keeps time-to-tile mapping explicit, including the sheet-local row offset.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import replace
import math
from threading import RLock
import time
from urllib.parse import urlparse

from murasamebot.domain.models import FrameContext, ImageRef, PlatformError, stable_id


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
    def __init__(self, request, *, count=6, max_parts=3, cache_minutes=1440, retry_minutes=10, clock=time.time):
        self.request, self.clock = request, clock
        self.count, self.max_parts = frame_policy(count, max_parts)
        self.cache_seconds, self.retry_seconds = cache_minutes * 60, retry_minutes * 60
        self._cache = OrderedDict()
        self._lock = RLock()

    def read(self, video: dict) -> tuple[tuple[ImageRef, ...], FrameContext]:
        now = self.clock()
        bvid = video.get("bvid")
        pages = video.get("pages") or [{"cid": video.get("cid"), "duration": video.get("duration")}]
        if not isinstance(pages, list):
            pages = []
        key = stable_id(bvid, pages[:3], self.count, self.max_parts)
        with self._lock:
            old = self._cache.get(key)
            if old and now < old[0]:
                self._cache.move_to_end(key)
                return old[1]
        # Never hold the cache lock across network I/O: one slow video's
        # optional previews must not serialize unrelated conversations.
        result = self._read(bvid, pages, now)
        context = result[1]
        until = context.retry_after if context.status != "complete" else now + self.cache_seconds
        with self._lock:
            self._cache[key] = until, result
            while len(self._cache) > 64:
                self._cache.popitem(last=False)
            return result

    def _read(self, bvid, pages, now):
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
            try:
                shot, _ = self.request("/x/player/videoshot", params={"bvid": bvid, "cid": cid, "index": 1})
            except PlatformError as exc:
                # Optional absence degrades. Authentication, risk and real read
                # cooldown/quiet time retain the existing shared protections.
                if exc.pause or exc.retry_at or exc.code in {"-509", "read_limited", "read_deferred"}:
                    raise
                limits.append(f"分P{part}预览快照不可用")
                continue
            duration = page.get("duration", 0)
            if type(duration) not in {int, float} or not math.isfinite(duration) or duration < 0:
                duration = 0
            frames = sample_shot(shot, quota, duration, part)
            if frames:
                sampled.append(part)
                images.extend(frames)
            if len(frames) < quota:
                limits.append(f"分P{part}计划{quota}帧，取得{len(frames)}帧；快照可能缺失或格式异常")
        status = "complete" if len(images) == self.count and not limits else "partial" if images else "unavailable"
        limits.append("仅为低清、离散时间点的播放器预览截图，未完整观看，不包含声音或连续动作")
        # Deliberately omitted later parts are a coverage limit, not a failed
        # acquisition to repeat every ten minutes.
        retry = self.retry_seconds if len(images) < self.count else self.cache_seconds
        context = FrameContext(status, self.count, self.max_parts, len(pages), tuple(sampled), tuple(limits),
                               now, now + retry)
        return tuple(replace(image, index=i + 1) for i, image in enumerate(images)), context
