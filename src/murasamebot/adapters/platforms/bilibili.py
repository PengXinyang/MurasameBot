from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import replace
from hashlib import md5
from io import BytesIO
from html import unescape
import json
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
import time
from urllib.parse import quote, urlencode, urlparse

import httpx
from PIL import Image

from murasamebot.adapters.network import client as network_client
from murasamebot.adapters.platforms.bilibili_private import BilibiliPrivateMixin
from murasamebot.adapters.platforms.bilibili_comments import read_comment_chain
from murasamebot.adapters.platforms.bilibili_frames import VideoShotReader
from murasamebot.adapters.platforms.bilibili_emojis import BilibiliEmojiMixin
from murasamebot.adapters.platforms.bilibili_publication import BilibiliPublicationMixin, classify_comment

from murasamebot.domain.models import Action, ContentRef, Document, Event, ImageRef, PlatformError as CorePlatformError, Receipt, stable_id
from murasamebot.domain.topics import local_topic


API = "https://api.bilibili.com"
PASSPORT = "https://passport.bilibili.com"
COMMENT_TYPES = {"video": 1, "dynamic": 17, "draw": 11, "article": 12}
TYPE_KINDS = {value: key for key, value in COMMENT_TYPES.items()}
WEB_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Referer": "https://www.bilibili.com/",
    "Accept": "application/json, text/plain, */*",
}
CREDENTIAL_FIELDS = {"SESSDATA", "bili_jct", "DedeUserID", "buvid3", "buvid4", "ac_time_value"}
# Compatibility catalog for these community endpoints, not a claim that the website's
# current navigation is identical. Current content responses provide tid/tname for inspection.
REGIONS = {
    "1": "动画", "3": "音乐", "4": "游戏", "5": "娱乐", "36": "知识",
    "119": "鬼畜", "129": "舞蹈", "155": "时尚", "160": "生活", "181": "影视",
    "188": "科技", "202": "资讯", "211": "美食", "217": "动物圈", "223": "汽车", "234": "运动",
}
MIXIN_INDEX = (46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,27,43,5,49,33,9,42,19,29,28,14,39,12,38,41,13,37,48,7,16,24,55,40,61,26,17,0,1,60,51,30,4,22,25,54,21,56,59,6,63,57,62,11,36,20,34,44,52)


class PlatformError(CorePlatformError):
    def __init__(self, message, *, code="platform_error", pause=False, retry_at=0):
        super().__init__(message, code=code, pause=pause, platform="bilibili",retry_at=retry_at)


def plain(value: object) -> str:
    return unescape(re.sub(r"<[^>]*>", "", str(value or "")))


def numeric(value: object) -> str:
    text = str(value)
    if not re.fullmatch(r"\d{1,24}", text):
        raise PlatformError("无效的平台数字标识")
    return text


class BilibiliAdapter(BilibiliPrivateMixin, BilibiliEmojiMixin, BilibiliPublicationMixin):
    """Bilibili protocol details stay here. No dependency on an archived SDK."""

    def __init__(self, credentials_path: Path, *, expected_uid: str = "", pages: int = 3,
                 max_age_hours: int = 24, client: httpx.Client | None = None,
                 proxy_url: str = "", image_client: httpx.Client | None = None,
                 private_session_pages: int = 3, private_message_pages: int = 3, private_max_age_hours: int = 24,
                 read_video_subtitles: bool = True, inbox_batch_limit: int = 90, proactive_recipient_id: str = "", monitor=None,
                 video_frames_enabled: bool = False, video_frame_count: int = 6, video_frame_max_parts: int = 3,
                 frame_cache_minutes: int = 1440, frame_retry_minutes: int = 10):
        self.monitor = monitor
        self.credentials_path = credentials_path
        self.expected_uid = str(expected_uid or "")
        self.pages = max(1, min(pages, 20))
        self.max_age = max_age_hours * 3600
        self.proxy_url = proxy_url
        self.image_client = image_client
        self.private_session_pages = private_session_pages
        self.private_message_pages = private_message_pages
        self.private_max_age_hours = private_max_age_hours
        self.read_video_subtitles = read_video_subtitles
        self.inbox_batch_limit = inbox_batch_limit
        self.proactive_recipient_id = str(proactive_recipient_id)
        self.poll_warnings = []

        self.client = client or network_client(proxy_url=proxy_url, monitor=monitor, headers=WEB_HEADERS,
                                               timeout=httpx.Timeout(25, connect=10))
        if client is not None:
            self.client.headers.update(WEB_HEADERS)
        self.credentials = self._load_credentials()
        self._sync_cookies()
        self._wbi_key: tuple[str, float] | None = None
        self._account: dict | None = None
        self.video_frames_enabled = video_frames_enabled
        self.video_frames = VideoShotReader(self._request, count=video_frame_count, max_parts=video_frame_max_parts,
                                            cache_minutes=frame_cache_minutes, retry_minutes=frame_retry_minutes)

    def warn_inbox(self, source: str, detail: str):
        self.poll_warnings.append({"source": source, "detail": detail})

    def take_warnings(self):
        warnings, self.poll_warnings = self.poll_warnings, []
        return warnings

    def _sync_cookies(self):
        for key, value in self.credentials.items():
            for host in ("api.bilibili.com", "api.vc.bilibili.com"):
                self.client.cookies.set(key, value, domain=host, path="/")

    def _load_credentials(self) -> dict:
        if not self.credentials_path.exists():
            return {}
        data = json.loads(self.credentials_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("登录凭证必须为 JSON 对象")
        return {k: str(v) for k, v in data.items() if k in CREDENTIAL_FIELDS and isinstance(v, str) and v}

    @property
    def configured(self) -> bool:
        return bool(self.credentials.get("SESSDATA") and self.credentials.get("bili_jct"))

    @property
    def account_id(self) -> str:
        return self.expected_uid or self.credentials.get("DedeUserID", "") or (self._account or {}).get("uid", "")

    def _request(self, path: str, *, params=None, data=None, signed=False):
        url = path if path.startswith("https://") else API + path
        host = urlparse(url).hostname or ""
        if host not in {"api.bilibili.com", "api.vc.bilibili.com", "passport.bilibili.com"}:
            raise PlatformError("不允许向任意域名发送平台请求")
        if signed:
            params = self._signed(dict(params or {}))
        try:
            response = self.client.request("POST" if data is not None else "GET", url,
                                           params=params, data=data)
            if response.status_code in {403, 412, 429}:
                read_limit = response.status_code==429 and data is None
                raise PlatformError(f"平台限制访问（HTTP {response.status_code}）", code="read_limited" if read_limit else "risk_control", pause=not read_limit,
                                    retry_at=self.monitor.retry_at(urlparse(url).path) if read_limit and self.monitor else 0)
            if response.status_code != 200:
                raise PlatformError(f"平台 HTTP {response.status_code}")
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("平台响应必须为对象")
        except (httpx.HTTPError, ValueError):
            raise PlatformError("平台连接或响应解析失败") from None
        code = payload.get("code", -1)
        if code != 0:
            pause = code in {-101, -111, -403, -412, -352, -401, 12015} or code==-509 and data is not None
            raise PlatformError(f"平台接口错误 {code}", code=str(code), pause=pause,
                                retry_at=self.monitor.retry_at(urlparse(url).path) if code==-509 and self.monitor and data is None else 0)
        result = payload.get("data") or {}
        if not isinstance(result, dict):
            raise PlatformError("平台数据格式无效")
        return result, response

    def account(self) -> dict:
        data, _ = self._request("/x/web-interface/nav")
        if not data.get("isLogin"):
            raise PlatformError("B 站尚未登录或登录已过期", code="auth_expired", pause=True)
        uid = str(data.get("mid", ""))
        if self.expected_uid and uid != self.expected_uid:
            raise PlatformError("实际登录账号与配置的机器人 UID 不一致", code="wrong_account", pause=True)
        if self.credentials.get("DedeUserID") and uid != self.credentials["DedeUserID"]:
            raise PlatformError("登录凭证中的 UID 不一致", code="wrong_account", pause=True)
        self._account = {"uid": uid, "name": str(data.get("uname", "")), "money": data.get("money")}
        self._cache_wbi_key(data)
        return self._account

    def read_account(self) -> dict:
        if self.monitor is None:
            return self.account()
        key="bili_identity_cache:"+stable_id(self.credentials)
        cached=self.monitor.store.get_setting(key,{})
        if cached.get("until",0)>self.monitor.clock():
            account=cached.get("account",{})
            expected=self.expected_uid or self.credentials.get("DedeUserID","")
            if expected and account.get("uid")==expected:
                self._account=account
                return account
        account=self.account()
        self.monitor.store.set_setting(key,{"account":account,"until":self.monitor.clock()+self.monitor.settings.account_cache_seconds})
        return account

    def _cache_wbi_key(self, nav: dict) -> bool:
        wbi = nav.get("wbi_img")
        if not isinstance(wbi, dict):
            return False
        keys = [urlparse(str(wbi.get(key) or "")).path.rsplit("/", 1)[-1].split(".")[0]
                for key in ("img_url", "sub_url")]
        if not all(re.fullmatch(r"[0-9a-fA-F]{32}", key) for key in keys):
            return False
        original = "".join(keys)
        self._wbi_key = ("".join(original[i] for i in MIXIN_INDEX)[:32], time.time() + 1800)
        return True

    def _signed(self, params):
        if not self._wbi_key or self._wbi_key[1] < time.time():
            nav, _ = self._request("/x/web-interface/nav")
            if not self._cache_wbi_key(nav):
                raise PlatformError("无法取得站内接口签名参数")
        params = dict(params)
        params.pop("w_rid", None)
        params["wts"] = int(time.time())
        cleaned = {str(k): re.sub(r"[!'()*]", "", str(v)) for k, v in sorted(params.items())}
        cleaned["w_rid"] = md5((urlencode(cleaned, quote_via=quote) + self._wbi_key[0]).encode()).hexdigest()
        return cleaned

    def generate_login(self) -> dict:
        data, _ = self._request(PASSPORT + "/x/passport-login/web/qrcode/generate")
        return {"url": data["url"], "key": data["qrcode_key"]}

    def poll_login(self, key: str) -> str:
        data, response = self._request(PASSPORT + "/x/passport-login/web/qrcode/poll", params={"qrcode_key": key})
        status = int(data.get("code", -1))
        if status == 0:
            # Persist server-issued cookies received during this login session,
            # not only the final poll's Set-Cookie. Do not invent device IDs or
            # retain another account's auth/refresh fields after switching users.
            credentials = {key: self.credentials[key] for key in ("buvid3", "buvid4") if self.credentials.get(key)}
            for jar in (self.client.cookies.jar, response.cookies.jar):
                for cookie in jar:
                    if (cookie.name in CREDENTIAL_FIELDS and cookie.value
                            and cookie.domain.lstrip(".") in {"bilibili.com", "passport.bilibili.com", "api.bilibili.com"}):
                        credentials[cookie.name] = cookie.value
            # Final confirmation must supply fresh account auth, not stale
            # cookies from a previous login still held by the HTTP client.
            confirmed = {cookie.name: cookie.value for cookie in response.cookies.jar
                         if cookie.name in CREDENTIAL_FIELDS and cookie.domain.lstrip(".") in {"bilibili.com", "passport.bilibili.com", "api.bilibili.com"}}
            for key in ("SESSDATA", "bili_jct", "DedeUserID", "ac_time_value"):
                credentials.pop(key, None)
                if confirmed.get(key):
                    credentials[key] = confirmed[key]
            if not credentials.get("SESSDATA") or not credentials.get("bili_jct"):
                raise PlatformError("扫码成功但未取得完整 Cookie")
            self.credentials_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.credentials_path.with_suffix(".tmp")
            temp.write_text(json.dumps(credentials, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.chmod(0o600)
            temp.replace(self.credentials_path)
            self.credentials = credentials
            self._sync_cookies()
            self.account()
            return "success"
        return {86038: "expired", 86090: "scanned", 86101: "waiting"}.get(status, "error")

    @staticmethod
    def notification_context(source: dict) -> ContentRef | None:
        try:
            url = urlparse(str(source.get("uri") or ""))
            valid = url.scheme == "https" and not url.username and url.port in {None, 443}
        except ValueError:
            return None
        if not valid:
            return None
        pattern = r"/opus/(\d{1,24})/?" if url.hostname == "www.bilibili.com" else r"/(\d{1,24})/?" if url.hostname == "t.bilibili.com" else ""
        match = re.fullmatch(pattern, url.path) if pattern else None
        return ContentRef("bilibili", "post", match[1]) if match else None

    @staticmethod
    def notification_event(item: dict, kind: str, account_id: str, now: float) -> Event | None:
        source, user = item.get("item") or {}, item.get("user") or {}
        rpid = str(source.get("source_id") or "")
        oid = str(source.get("subject_id") or "")
        business = int(source.get("business_id") or 0)
        actor = str(user.get("mid") or "")
        if not actor.isdigit() or actor == account_id:
            return None
        context = BilibiliAdapter.notification_context(source)
        if (kind == "mention" and context and source.get("type") in {"dynamic", "opus"} and
                (not rpid.isdigit() or rpid == "0" or rpid == context.external_id)):
            return Event(f"bilibili:{account_id}:post_mention:{context.external_id}:{actor}", "bilibili", account_id, actor,
                         f"bilibili:{account_id}:{context.key}", "post_mention", plain(source.get("source_content") or source.get("title")),
                         float(item.get("at_time") or now), content=context, share_content=context)
        if not rpid.isdigit() or not oid.isdigit() or business not in TYPE_KINDS:
            return None
        ref = ContentRef("bilibili", TYPE_KINDS[business], oid)
        root = str(source.get("root_id") or rpid)
        return Event(f"bilibili:{account_id}:comment:{business}:{oid}:{rpid}", "bilibili", account_id, actor,
                     f"bilibili:{account_id}:{ref.key}:thread:{root}", kind,
                     plain(source.get("source_content", "")),
                     float(item.get("at_time") or item.get("reply_time") or now),
                     content=ref, reply_to=rpid, thread_root=root, actor_name=plain(user.get("nickname")),
                     share_content=context)

    def _resolve_post_mention(self, event: Event) -> Event | None:
        data, _ = self._request("/x/polymer/web-dynamic/v1/detail", params={"id": numeric(event.content.external_id)})
        item = data.get("item") or {}
        author = ((item.get("modules") or {}).get("module_author") or {}).get("mid")
        ref = self.dynamic_reference(item)
        if not ref or str(author) != event.actor_id:
            return None
        return replace(event, content=ref)

    def notifications(self, now: float) -> list[Event]:
        uid = (self._account or self.read_account())["uid"]
        events = {}
        for endpoint, kind in (("/x/msgfeed/reply", "reply"), ("/x/msgfeed/at", "mention")):
            params = {"ps": 30, "platform": "web"}
            for _ in range(self.pages):
                data, _ = self._request(endpoint, params=params)
                items = data.get("items") or []
                for item in items:
                    event = self.notification_event(item, kind, uid, now)
                    if event and event.occurred_at >= now - self.max_age:
                        if event.kind == "post_mention":
                            try:
                                event = self._resolve_post_mention(event)
                            except PlatformError as exc:
                                if exc.pause:
                                    raise
                                continue
                            if event is None:
                                continue
                        events[event.event_id] = event
                cursor = data.get("cursor") or {}
                if not items or cursor.get("is_end", True):
                    break
                params.update({"id": cursor.get("id", items[-1].get("id", 0)),
                               "reply_time" if kind == "reply" else "at_time": cursor.get("time", 0)})
            else:
                # Do not silently pretend an unbounded backlog has been fully consumed.
                self.warn_inbox("notifications", f"{'回复' if kind == 'reply' else '@'}通知超过 {self.pages} 页，本轮保留已读取的消息；较旧消息未覆盖。")
        latest = sorted(events.values(), key=lambda event: (event.occurred_at, event.event_id), reverse=True)
        if len(latest) > self.inbox_batch_limit:
            self.warn_inbox("notifications", f"本轮评论/@通知超过 {self.inbox_batch_limit} 条，仅接收最新 {self.inbox_batch_limit} 条；超出范围不保证补回。")
        return sorted(latest[:self.inbox_batch_limit], key=lambda event: event.occurred_at)

    @staticmethod
    def dynamic_reference(item: dict) -> ContentRef | None:
        basic = item.get("basic") or {}
        oid, type_ = str(basic.get("comment_id_str") or ""), int(basic.get("comment_type") or 0)
        if oid.isdigit() and type_ in TYPE_KINDS:
            return ContentRef("bilibili", TYPE_KINDS[type_], oid)
        dynamic = (item.get("modules") or {}).get("module_dynamic") or {}
        archive = (dynamic.get("major") or {}).get("archive") or {}
        aid = str(archive.get("aid") or "")
        if aid.isdigit():
            return ContentRef("bilibili", "video", aid)
        # Do not guess image-dynamic comment IDs from dynamic IDs.
        if item.get("type") == "DYNAMIC_TYPE_WORD" and str(item.get("id_str", "")).isdigit():
            return ContentRef("bilibili", "dynamic", item["id_str"])
        return None

    def owner_content(self, owner_external_id: str, now: float) -> list[Event]:
        uid = (self._account or self.read_account())["uid"]
        data, _ = self._request("/x/polymer/web-dynamic/v1/feed/space", params={"host_mid": numeric(owner_external_id), "offset": "", "timezone_offset": -480})
        result = []
        for item in data.get("items") or []:
            ref = self.dynamic_reference(item)
            author = (item.get("modules") or {}).get("module_author") or {}
            if str(author.get("mid", "")) != owner_external_id:
                continue
            post_id = str(item.get("id_str", ""))
            share = ContentRef("bilibili", "post", post_id) if post_id.isdigit() else None
            if ref is None:
                # An unsupported comment type may still support a verified native repost.
                if share:
                    result.append(Event(f"bilibili:{uid}:owner:" + share.key, "bilibili", uid, owner_external_id,
                                        "bilibili:" + share.key, "owner_share_only", "主人发布了新动态", float(author.get("pub_ts") or now),
                                        content=share, share_content=share))
                continue
            result.append(Event(f"bilibili:{uid}:owner:" + ref.key, "bilibili", uid, owner_external_id,
                                "bilibili:" + ref.key, "owner_content", "主人发布了新内容", float(author.get("pub_ts") or now), content=ref,
                                share_content=share))
        return sorted(result, key=lambda event: event.occurred_at)

    def _comments(self, ref: ContentRef, reply_to: str, root: str) -> tuple[dict[str, str], ...]:
        type_ = COMMENT_TYPES[ref.kind]
        comments = []
        if reply_to:
            return self._comment_chain(ref, reply_to, root)[0]
        data, _ = self._request("/x/v2/reply/main", params={"oid": numeric(ref.external_id), "type": type_, "mode": 3, "ps": 5})
        rows = data.get("replies") or []
        for row in rows:
            author_id = str((row.get("member") or {}).get("mid") or "")
            if author_id and author_id == self.account_id:
                continue
            identifier = str(row.get("rpid_str") or row.get("rpid", ""))
            parent = str(row.get("root_str") or row.get("root") or "")
            comments.append({"id": identifier,
                             "root": parent if parent not in {"", "0"} else identifier,
                             "author_id": author_id,
                             "text": str((row.get("content") or {}).get("message", ""))[:1500]})
        return tuple(comments)

    def _comment_chain(self, ref: ContentRef, reply_to: str, root: str):
        return read_comment_chain(self._request, oid=numeric(ref.external_id), type_=COMMENT_TYPES[ref.kind],
                                  target=numeric(reply_to), root=numeric(root or reply_to), account_id=self.account_id)

    def _subtitle(self, bvid: str, cid: str) -> str:
        data, _ = self._request("/x/player/v2", params={"bvid": bvid, "cid": cid})
        tracks = (data.get("subtitle") or {}).get("subtitles") or []
        tracks.sort(key=lambda track: 0 if "zh" in track.get("lan", "") else 1)
        for track in tracks[:1]:
            url = str(track.get("subtitle_url") or "")
            if url.startswith("//"):
                url = "https:" + url
            host = urlparse(url).hostname or ""
            if urlparse(url).scheme != "https" or not (host.endswith(".hdslb.com") or host.endswith(".bilibili.com")):
                continue
            try:
                # Explicitly use a separate cookie-free client for content CDNs.
                with network_client(proxy_url=self.proxy_url, timeout=20) as reader:
                    response = reader.get(url)
                    if response.status_code != 200 or len(response.content) > 4_000_000:
                        continue
                    lines = response.json().get("body") or []
                return "\n".join(str(line.get("content", "")) for line in lines)[:20000]
            except (httpx.HTTPError, ValueError):
                continue
        return ""

    def read(self, ref: ContentRef, *, reply_to="", root="", context: ContentRef | None = None, topic_filter=True) -> Document:
        if ref.provider != "bilibili" or ref.kind not in set(COMMENT_TYPES) | {"post"}:
            raise PlatformError("不支持的内容引用")
        oid = numeric(ref.external_id)
        frame_images, frame_context = (), None
        if ref.kind == "video":
            data, _ = self._request("/x/web-interface/view", params={"aid": oid})
            bvid = str(data.get("bvid", ""))
            if topic_filter and local_topic(plain(data.get("title")),data.get("desc"),data.get("tname")) == "sensitive":
                return Document(ref,plain(data.get("title")),str(data.get("desc") or "")[:3000],"metadata",
                                author_id=str((data.get("owner") or {}).get("mid", "")),
                                category_id=str(data.get("tid", "")),topic_class="sensitive")
            transcripts = []
            pages = (data.get("pages") or [{"cid": data.get("cid")}])[:3] if self.read_video_subtitles else []
            for page in pages:
                cid = str(page.get("cid") or "")
                if cid.isdigit():
                    try:
                        text = self._subtitle(bvid, cid)
                    except CorePlatformError as exc:
                        if exc.pause:
                            raise
                        # Optional subtitles must not prevent reading known metadata.
                        text = ""
                    if text:
                        transcripts.append(str(page.get("part") or "") + "\n" + text)
            transcript = "\n".join(transcripts)[:16000]
            level = "transcript" if transcript else "metadata"
            description = str(data.get("desc") or "")[:3000]
            body = ("视频简介：\n" + description + "\n取得的字幕（最多前3个分P，可能不完整）：\n" + transcript) if transcript else description
            title, author, category = plain(data.get("title")), str((data.get("owner") or {}).get("mid", "")), str(data.get("tid", ""))
            url = "https://www.bilibili.com/video/" + bvid
            if self.video_frames_enabled and (not topic_filter or local_topic(title, body) != "sensitive"):
                frame_images, frame_context = self.video_frames.read(data)
        else:
            if ref.kind in {"dynamic", "post", "draw"}:
                post_id = oid
                if ref.kind == "draw":
                    if context is None or context.provider != "bilibili" or context.kind != "post":
                        raise PlatformError("图文通知缺少原动态引用，不能把评论区 ID 当作动态 ID", code="missing_dynamic_context")
                    post_id = numeric(context.external_id)
                data, _ = self._request("/x/polymer/web-dynamic/v1/detail", params={"id": post_id})
                item = data.get("item") or {}
                if ref.kind == "draw":
                    basic = item.get("basic") or {}
                    if str(basic.get("comment_id_str") or "") != oid or basic.get("comment_type") != COMMENT_TYPES["draw"]:
                        raise PlatformError("原动态与通知中的图文评论区不匹配", code="content_reference_mismatch")
                dynamic = (item.get("modules") or {}).get("module_dynamic") or {}
                opus = (dynamic.get("major") or {}).get("opus") or {}
                body = str((dynamic.get("desc") or {}).get("text") or (opus.get("summary") or {}).get("text") or "")
                if ref.kind == "post" and not body:
                    archive = (dynamic.get("major") or {}).get("archive") or {}
                    body = str(archive.get("title") or "")
                author = str(((item.get("modules") or {}).get("module_author") or {}).get("mid", ""))
                original = item.get("orig") or {}
                original_dynamic = ((original.get("modules") or {}).get("module_dynamic") or {})
                original_major = original_dynamic.get("major") or {}
                original_text = (original_dynamic.get("desc") or {}).get("text") or ((original_major.get("opus") or {}).get("summary") or {}).get("text") or ""
                if original_text:
                    body += "\n转发原文：" + str(original_text)
            else:
                raise PlatformError("专栏通知可识别，正文读取尚不支持", code="unsupported_content")
            title, category, url = "动态", "", "https://www.bilibili.com/opus/" + post_id
            level = "text" if body else "metadata"
        coverage = None
        if reply_to and ref.kind != "post":
            comments, coverage = self._comment_chain(ref, reply_to, root)
        else:
            comments = self._comments(ref, "", "") if ref.kind != "post" else ()
        images = self.dynamic_images(item) if ref.kind in {"dynamic", "post", "draw"} else frame_images
        parent = ContentRef("bilibili", "post", post_id) if ref.kind in {"dynamic", "post", "draw"} else None
        return Document(ref, title, body[:20000], level, author, category, url, comments, images, parent=parent,
                        comment_context=coverage, frame_context=frame_context)

    @staticmethod
    def dynamic_images(item: dict) -> tuple[ImageRef, ...]:
        urls = []
        for post in (item, item.get("orig") or {}):
            dynamic = ((post.get("modules") or {}).get("module_dynamic") or {})
            major = dynamic.get("major") or {}
            rows = ((major.get("draw") or {}).get("items") or []) + ((major.get("opus") or {}).get("pics") or [])
            for row in rows:
                url = str(row.get("src") or row.get("url") or "")
                if url.startswith("//"):
                    url = "https:" + url
                elif url.startswith("http://"):
                    url = "https://" + url[7:]
                if url and url not in urls:
                    urls.append(url)
        return tuple(ImageRef(url, index + 1) for index, url in enumerate(urls))

    def discover(self, category_ids: Sequence[str], limit: int) -> list[ContentRef]:
        refs = {}
        for category in category_ids:
            data, _ = self._request("/x/web-interface/newlist", params={"rid": numeric(category), "pn": 1, "ps": min(limit, 30)})
            for row in data.get("archives") or []:
                if local_topic(plain(row.get("title")),row.get("desc"),row.get("tname")) == "sensitive":
                    continue
                aid = str(row.get("aid") or "")
                if aid.isdigit():
                    ref = ContentRef("bilibili", "video", aid)
                    refs[ref.key] = ref
        return list(refs.values())[:limit]

    def search(self, query: str, category_ids: Sequence[str], limit: int, *, topic_filter=True) -> list[ContentRef]:
        if topic_filter and local_topic(query) == "sensitive":
            return []
        refs = {}
        for category in list(category_ids) or ["0"]:
            data, _ = self._request("/x/web-interface/wbi/search/type", signed=True, params={
                "search_type": "video", "keyword": query[:120], "tids": numeric(category), "page": 1,
                "page_size": min(limit, 20), "order": "totalrank",
            })
            for row in data.get("result") or []:
                if topic_filter and local_topic(plain(row.get("title")),row.get("description"),row.get("typename")) == "sensitive":
                    continue
                aid = str(row.get("aid") or "")
                if aid.isdigit():
                    ref = ContentRef("bilibili", "video", aid)
                    refs[ref.key] = ref
        return list(refs.values())[:limit]

    def _mention_name(self, uid: str) -> str:
        data, _ = self._request("/x/web-interface/card", params={"mid": numeric(uid)})
        name = str((data.get("card") or {}).get("name") or "")
        if not name or any(char in name for char in "\n\r"):
            raise PlatformError("无法解析目标账号当前昵称，未发送提及")
        return name

    def send(self, action: Action, mentions: Mapping[str, str], *, before_write: Callable[[], bool] | None = None) -> Receipt:
        if not self.configured:
            raise PlatformError("请先扫码登录", code="not_logged_in", pause=True)
        account = self.account()  # Revalidate before every write.
        if action.account_id != account["uid"]:
            raise PlatformError("排队动作属于不同的机器人账号", code="wrong_account", pause=True)
        emoji_error = self.check_emojis(action)
        if emoji_error:
            return emoji_error
        if action.kind in {"like", "coin"}:
            return self._send_reaction(action, before_write=before_write)
        if action.kind in {"private_reply", "proactive_private"}:
            with self.monitor.essential_reads() if self.monitor else nullcontext():
                return self._send_private(action, before_write=before_write)
        if action.kind in {"owner_repost", "artwork_share", "autonomous_post"}:
            return self._send_dynamic(action, before_write=before_write)
        if action.content is None or action.content.kind not in COMMENT_TYPES:
            raise PlatformError("B 站适配器不支持此发送目标")
        names = {self._mention_name(uid): int(uid) for uid in mentions.values()}
        message = "".join(f"@{name} " for name in names) + action.text
        fields = {"oid": numeric(action.content.external_id), "type": COMMENT_TYPES[action.content.kind],
                  "message": message, "csrf": self.credentials["bili_jct"], "plat": 1,
                  "statistics": json.dumps({"appId": 100, "platform": 5}, separators=(",", ":")),
                  "gaia_source": "main_web", "at_name_to_mid": json.dumps(names, ensure_ascii=False)}
        if action.reply_to:
            fields.update({"root": numeric(action.thread_root or action.reply_to), "parent": numeric(action.reply_to)})
        # The captured successful video request is signed even though its path
        # has no /wbi segment. Do not reuse captured timestamps or environment
        # tokens. Other comment surfaces retain their existing signing policy.
        params = self._signed({}) if action.content.kind == "video" else None
        headers = {"Origin": "https://www.bilibili.com", "Accept-Language": "zh-CN,zh;q=0.9"}
        if action.content.kind == "video":
            headers["Referer"] = f"https://www.bilibili.com/video/av{numeric(action.content.external_id)}/"
        if before_write is not None and not before_write():
            return Receipt("failed", detail="发送前的最终检查未通过：任务、绑定或运行状态已经改变")
        try:
            response = self.client.post(API + "/x/v2/reply/add", params=params, data=fields, headers=headers)
            if response.status_code != 200:
                # An intermediary or invalid response may have hidden successful acceptance.
                return Receipt("unknown", detail=f"写请求返回 HTTP {response.status_code}，需要人工核对", pause=response.status_code in {403, 412, 429})
            data = response.json()
        except (httpx.HTTPError, ValueError):
            return Receipt("unknown", detail="写请求超时或响应不可解析，禁止自动重发")
        if data.get("code") != 0:
            code = data.get("code")
            pause = code in {-101, -111, -403, -412, -509, -352, -401, 12015}
            raise PlatformError(f"评论未受理，接口错误 {code}", code=str(code), pause=pause)
        payload = data.get("data") or {}
        reply = payload.get("reply") or {}
        rpid = str(reply.get("rpid_str") or reply.get("rpid") or payload.get("rpid", ""))
        if not rpid:
            return Receipt("unknown", detail="平台返回成功但没有可核对的评论 ID")
        resolved = {str(member.get("mid")) for member in ((reply.get("content") or {}).get("members") or [])}
        mention_resolved = all(str(mid) in resolved for mid in names.values())
        return Receipt("accepted", rpid, "平台已受理，公开可见性尚未独立验证", mention_resolved,
                       publication_check=classify_comment(reply))

    def _ensure_reaction_device(self):
        # A QR session can contain valid account auth without a web device
        # cookie. Obtain one from the platform, never synthesize fingerprints
        # or rotate an existing device after a rejected write.
        if self.credentials.get("buvid3"):
            return
        valid = lambda value: isinstance(value, str) and 0 < len(value) <= 512 and re.fullmatch(r"[A-Za-z0-9._=+/-]+", value)
        # Each sender job owns a new adapter. Keep a separate, session-bound
        # device cache so it does not request a different device every send,
        # and never rewrite or race the account's QR-login credential file.
        cache_path = self.credentials_path.with_suffix(".device.json")
        signature = stable_id(*(self.credentials.get(key, "") for key in ("SESSDATA", "bili_jct", "DedeUserID")))
        values = {}
        if cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                if not isinstance(cached, dict):
                    raise ValueError()
                if cached.get("session_signature") == signature:
                    values = {key: cached.get(key) for key in ("buvid3", "buvid4")}
                    if not valid(values["buvid3"]):
                        raise ValueError()
            except (ValueError, OSError):
                raise PlatformError("网页设备缓存无效，未轮换设备或发送互动；请检查本地凭证或重新登录", code="device_cookie_missing", pause=True) from None
        if not values:
            device, _ = self._request("/x/frontend/finger/spi")
            values = {key: device.get(field) for key, field in (("buvid3", "b_3"), ("buvid4", "b_4"))}
        if not valid(values["buvid3"]):
            raise PlatformError("未取得B站签发的网页设备凭证buvid3，已停止点赞/投币；请检查登录与平台验证", code="device_cookie_missing", pause=True)
        device_values = {key: value for key, value in values.items() if valid(value)}
        temporary = None
        try:
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=cache_path.parent,
                                    prefix=cache_path.name + ".", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                temporary.chmod(0o600)
                json.dump({"session_signature": signature, **device_values}, handle)
            temporary.replace(cache_path)
        except OSError:
            raise PlatformError("无法保存独立网页设备缓存，未发送互动；请检查凭证目录权限", code="device_cookie_missing", pause=True) from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        for key, value in values.items():
            if valid(value) and not self.credentials.get(key):
                self.credentials[key] = value
        self._sync_cookies()

    def _send_reaction(self, action: Action, *, before_write=None) -> Receipt:
        ref = action.content
        if not ref or ref.provider != "bilibili" or action.mentions or action.recipient_id:
            raise PlatformError("互动缺少有效内容引用，或包含不允许的接收对象")
        if action.kind == "coin" and (ref.kind != "video" or type(action.amount) is not int or action.amount != 1):
            raise PlatformError("只允许给视频投 1 枚硬币")
        if action.kind == "like" and action.amount != 0:
            raise PlatformError("点赞不允许携带投币数量")
        headers = None
        if ref.kind == "video":
            fields = {"aid": numeric(ref.external_id), "csrf": self.credentials["bili_jct"]}
            if action.kind == "coin":
                state, _ = self._request("/x/web-interface/archive/coins", params={"aid": fields["aid"]})
                count = state.get("multiply")
                if type(count) is not int or count < 0:
                    raise PlatformError("无法确认该视频已有投币数，未投币")
                if count >= 1:
                    return Receipt("accepted", f"coin:{fields['aid']}", "视频已经投过币，未重复投币")
                balance = self.account().get("money")
                if type(balance) not in {int, float} or not 0 <= balance < float("inf"):
                    raise PlatformError("无法确认硬币余额，未投币")
                if balance < 1:
                    return Receipt("failed", detail="硬币余额不足 1 枚，跳过投币")
                fields.update(multiply=1, select_like=0)
                path = "/x/web-interface/coin/add"
            else:
                # The combined relation endpoint returns an object rather than a scalar.
                state, _ = self._request("/x/web-interface/archive/relation", params={"aid": fields["aid"]})
                if state.get("like") is True or state.get("like") == 1:
                    return Receipt("accepted", f"like:{fields['aid']}", "视频已经点赞，未重复写入")
                fields["like"] = 1
                path = "/x/web-interface/archive/like"
            self._ensure_reaction_device()
            headers = {"Origin": "https://www.bilibili.com",
                       "Referer": f"https://www.bilibili.com/video/av{fields['aid']}/"}
            body = None
            target_id = fields["aid"]
        elif action.kind == "like" and ref.kind in {"dynamic", "draw", "post"}:
            parent = action.context or (ref if ref.kind == "post" else None)
            if parent is None or parent.provider != "bilibili" or parent.kind != "post":
                raise PlatformError("动态点赞需要实际动态 ID，不能使用图文评论区 ID")
            target_id = numeric(parent.external_id)
            result, _ = self._request("/x/polymer/web-dynamic/v1/detail", params={"id": target_id})
            item = result.get("item") or {}
            if str(item.get("id_str") or "") != target_id:
                raise PlatformError("动态点赞目标与读取的动态不匹配")
            if ref.kind != "post" and self.dynamic_reference(item) != ref:
                raise PlatformError("动态点赞目标与原评论区不匹配")
            like = ((item.get("modules") or {}).get("module_stat") or {}).get("like") or {}
            if like.get("status") is True:
                return Receipt("accepted", f"like:{target_id}", "动态已经点赞，未重复写入")
            path, fields = "/x/dynamic/feed/dyn/thumb", None
            body = {"dyn_id_str": target_id, "up": 1}
        else:
            raise PlatformError("不支持此类型的点赞或投币")
        if before_write is not None and not before_write():
            return Receipt("failed", detail="互动前最终检查未通过，未发送")
        _, receipt = self._write_json(path, data=fields, json_body=body, headers=headers)
        return receipt or Receipt("accepted", f"{action.kind}:{target_id}", "平台已受理点赞或单枚投币")

    def _image(self, artwork: dict) -> tuple[bytes, str]:
        url = urlparse(str(artwork.get("image_url", "")))
        host = url.hostname or ""
        source = (artwork.get("ref") or {}).get("provider")
        allowed = (source == "pixiv" and (host == "i.pximg.net" or host.endswith(".pximg.net"))) or (source == "x" and host == "pbs.twimg.com")
        if url.scheme != "https" or url.username or url.port not in {None, 443} or not allowed:
            raise PlatformError("图片地址不属于允许的来源 CDN")
        owned = self.image_client is None
        reader = self.image_client or network_client(proxy_url=self.proxy_url, timeout=30)
        try:
            with reader.stream("GET", url.geturl(), headers={"Referer": "https://www.pixiv.net/" if source == "pixiv" else "https://x.com/"}) as response:
                if response.status_code != 200:
                    raise PlatformError(f"图片下载失败 HTTP {response.status_code}")
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > 10_000_000:
                        raise PlatformError("图片超过 10 MB，未上传")
            with Image.open(BytesIO(data)) as picture:
                if picture.width * picture.height > 40_000_000 or picture.format not in {"JPEG", "PNG"}:
                    raise PlatformError("图片格式或像素尺寸不适合自动上传")
                suffix = "jpg" if picture.format == "JPEG" else "png"
                picture.verify()
            return bytes(data), suffix
        except (httpx.HTTPError, OSError, ValueError, Image.DecompressionBombError):
            raise PlatformError("图片下载或完整性验证失败") from None
        finally:
            if owned:
                reader.close()

    def _write_json(self, path: str, *, data=None, json_body=None, files=None, headers=None) -> tuple[dict, Receipt | None]:
        try:
            response = self.client.post(API + path, params={"csrf": self.credentials["bili_jct"]},
                                        data=data, json=json_body, files=files, headers=headers)
            if response.status_code != 200:
                return {}, Receipt("unknown", detail=f"写请求 HTTP {response.status_code}，禁止自动重发", pause=response.status_code in {403, 412, 429})
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError()
        except (httpx.HTTPError, ValueError):
            return {}, Receipt("unknown", detail="写请求结果无法确认，禁止自动重发")
        if "code" not in payload:
            return {}, Receipt("unknown", detail="写请求缺少平台状态码，禁止自动重发")
        if payload.get("code") != 0:
            code = payload.get("code")
            reasons = {-401: "请求未通过认证或平台校验，不是硬币不足；请检查登录、设备凭证及平台验证",
                       -104: "硬币余额不足", -101: "账号未登录或登录已过期",
                       -111: "CSRF校验失败，请重新登录", -352: "平台风控校验未通过",
                       34004: "投币间隔过短，请稍后再试", 34005: "该视频投币已达上限"}
            reason = "：" + reasons[code] if code in reasons else ""
            raise PlatformError(f"平台操作未受理，接口错误 {code}{reason}", code=str(code), pause=code in {-101, -111, -403, -412, -509, -352, -401, 12015})
        return payload.get("data") or {}, None

    def _send_dynamic(self, action: Action, *, before_write=None) -> Receipt:
        def allowed():
            return before_write is None or before_write()
        request = {"content": {"contents": [{"raw_text": action.text, "type": 1, "biz_id": ""}]},
                   "scene": 1, "meta": {"app_meta": {"from": "create.dynamic.web", "mobi_app": "web"}},
                   "upload_id": f"{action.account_id}_{int(action.created_at)}_{int(md5(action.action_id.encode()).hexdigest()[:8], 16) % 10000:04d}"}
        if action.emojis:
            contents = []
            # Render verified native emoji as rich-text nodes; never upload sticker files.
            tokens = {emoji.token for emoji in action.emojis if emoji.kind == "native"}
            pattern = "(" + "|".join(re.escape(token) for token in tokens) + ")" if tokens else ""
            for part in re.split(pattern, action.text) if pattern else [action.text]:
                if part:
                    contents.append({"raw_text": part, "type": 9 if part in tokens else 1, "biz_id": ""})
            request["content"]["contents"] = contents
        body = {"dyn_req": request}
        if action.kind == "owner_repost":
            if not action.content or action.content.provider != "bilibili" or action.content.kind != "post" or not action.source_author_id:
                raise PlatformError("转发需要原始动态引用与已验证作者")
            detail, _ = self._request("/x/polymer/web-dynamic/v1/detail", params={"id": numeric(action.content.external_id)})
            author = ((detail.get("item") or {}).get("modules") or {}).get("module_author") or {}
            if str(author.get("mid", "")) != action.source_author_id:
                raise PlatformError("转发前核验的作者与主人不一致")
            request["scene"] = 4
            body["web_repost_src"] = {"dyn_id_str": numeric(action.content.external_id)}
        elif action.kind == "autonomous_post":
            if action.visibility != "public" or action.content or action.mentions or action.artwork or action.recipient_id:
                raise PlatformError("自主文字动态包含不允许的来源或接收对象")
        else:
            work = action.artwork
            if not work or work.get("delivery") not in {"image", "link"}:
                raise PlatformError("缺少美图分享信息")
            ref = work.get("ref") or {}
            source = ref.get("provider")
            source_url = str(work.get("source_url", ""))
            expected = "https://www.pixiv.net/artworks/" + str(ref.get("external_id", "")) if source == "pixiv" else ""
            if (source == "pixiv" and source_url != expected) or (source == "x" and not re.fullmatch(r"https://x\.com/[A-Za-z0-9_]+/status/" + re.escape(str(ref.get("external_id", ""))), source_url)) or source not in {"pixiv", "x"}:
                raise PlatformError("作品原始链接无效")
            credit = re.sub(r"[\r\n@]", " ", str(work.get("artist_name", "")))[:80]
            request["content"]["contents"][0]["raw_text"] += f"\n画师：{credit}\n原作：{source_url}"
            if work["delivery"] == "image":
                if not work.get("permission_id") or not allowed():
                    return Receipt("failed", detail="上传前授权或最终检查未通过")
                image, suffix = self._image(work)
                if not allowed():
                    return Receipt("failed", detail="图片下载后授权已变化")
                uploaded, receipt = self._write_json("/x/dynamic/feed/draw/upload_bfs", data={"category": "daily", "csrf": self.credentials["bili_jct"]},
                                                     files={"file_up": ("artwork." + suffix, image, "image/jpeg" if suffix == "jpg" else "image/png")})
                if receipt:
                    return receipt
                if not uploaded.get("image_url"):
                    return Receipt("failed", detail="平台未返回上传图片地址，未创建动态")
                request["scene"] = 2
                request["pics"] = [{"img_src": uploaded["image_url"], "img_width": uploaded.get("image_width", 0),
                                    "img_height": uploaded.get("image_height", 0), "img_size": len(image) / 1024}]
        # Slow source validation, downloads and uploads must precede this final gate.
        if not allowed():
            return Receipt("failed", detail="发布动态前最终检查未通过")
        payload, receipt = self._write_json("/x/dynamic/feed/create/dyn", json_body=body)
        if receipt:
            return receipt
        dynamic_id = str(payload.get("dyn_id_str") or payload.get("dyn_id") or "")
        if not dynamic_id.isdigit():
            return Receipt("unknown", detail="平台返回成功但没有动态 ID，禁止重发")
        return Receipt("accepted", dynamic_id, "动态已受理，公开可见性尚未独立验证")

    def close(self):
        self.client.close()
