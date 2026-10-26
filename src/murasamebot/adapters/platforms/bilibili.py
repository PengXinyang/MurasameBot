from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from hashlib import md5
from io import BytesIO
from html import unescape
import json
from pathlib import Path
import re
import time
from urllib.parse import urlencode, urlparse

import httpx
from PIL import Image

from murasamebot.adapters.network import client as network_client

from murasamebot.domain.models import Action, ContentRef, Document, Event, PlatformError as CorePlatformError, Receipt


API = "https://api.bilibili.com"
PASSPORT = "https://passport.bilibili.com"
COMMENT_TYPES = {"video": 1, "dynamic": 17, "draw": 11, "article": 12}
TYPE_KINDS = {value: key for key, value in COMMENT_TYPES.items()}
# Compatibility catalog for these community endpoints, not a claim that the website's
# current navigation is identical. Current content responses provide tid/tname for inspection.
REGIONS = {
    "1": "动画", "3": "音乐", "4": "游戏", "5": "娱乐", "36": "知识",
    "119": "鬼畜", "129": "舞蹈", "155": "时尚", "160": "生活", "181": "影视",
    "188": "科技", "202": "资讯", "211": "美食", "217": "动物圈", "223": "汽车", "234": "运动",
}
MIXIN_INDEX = (46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,27,43,5,49,33,9,42,19,29,28,14,39,12,38,41,13,37,48,7,16,24,55,40,61,26,17,0,1,60,51,30,4,22,25,54,21,56,59,6,63,57,62,11,36,20,34,44,52)


class PlatformError(CorePlatformError):
    def __init__(self, message, *, code="platform_error", pause=False):
        super().__init__(message, code=code, pause=pause, platform="bilibili")


def plain(value: object) -> str:
    return unescape(re.sub(r"<[^>]*>", "", str(value or "")))


def numeric(value: object) -> str:
    text = str(value)
    if not re.fullmatch(r"\d{1,24}", text):
        raise PlatformError("无效的平台数字标识")
    return text


class BilibiliAdapter:
    """Bilibili protocol details stay here. No dependency on an archived SDK."""

    def __init__(self, credentials_path: Path, *, expected_uid: str = "", pages: int = 3,
                 max_age_hours: int = 24, client: httpx.Client | None = None,
                 proxy_url: str = "", image_client: httpx.Client | None = None):
        self.credentials_path = credentials_path
        self.expected_uid = str(expected_uid or "")
        self.pages = max(1, min(pages, 20))
        self.max_age = max_age_hours * 3600
        self.proxy_url = proxy_url
        self.image_client = image_client
        self.client = client or network_client(proxy_url=proxy_url, headers={
            "User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/",
        }, timeout=httpx.Timeout(25, connect=10))
        self.credentials = self._load_credentials()
        self._sync_cookies()
        self._wbi_key: tuple[str, float] | None = None
        self._account: dict | None = None

    def _sync_cookies(self):
        for key, value in self.credentials.items():
            self.client.cookies.set(key, value, domain="api.bilibili.com", path="/")

    def _load_credentials(self) -> dict:
        if not self.credentials_path.exists():
            return {}
        data = json.loads(self.credentials_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("登录凭证必须为 JSON 对象")
        return {k: str(v) for k, v in data.items() if k in {"SESSDATA", "bili_jct", "DedeUserID", "buvid3", "buvid4"}}

    @property
    def configured(self) -> bool:
        return bool(self.credentials.get("SESSDATA") and self.credentials.get("bili_jct"))

    @property
    def account_id(self) -> str:
        return self.expected_uid or self.credentials.get("DedeUserID", "") or (self._account or {}).get("uid", "")

    def _request(self, path: str, *, params=None, data=None, signed=False):
        url = path if path.startswith("https://") else API + path
        host = urlparse(url).hostname or ""
        if host not in {"api.bilibili.com", "passport.bilibili.com"}:
            raise PlatformError("不允许向任意域名发送平台请求")
        if signed:
            params = self._signed(dict(params or {}))
        try:
            response = self.client.request("POST" if data is not None else "GET", url,
                                           params=params, data=data)
            if response.status_code in {403, 412, 429}:
                raise PlatformError(f"平台限制访问（HTTP {response.status_code}），已停止出站", code="risk_control", pause=True)
            if response.status_code != 200:
                raise PlatformError(f"平台 HTTP {response.status_code}")
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            raise PlatformError("平台连接或响应解析失败") from None
        code = payload.get("code", -1)
        if code != 0:
            pause = code in {-101, -111, -403, -412, -509, -352, -401, 12015}
            raise PlatformError(f"平台接口错误 {code}", code=str(code), pause=pause)
        return payload.get("data") or {}, response

    def account(self) -> dict:
        data, _ = self._request("/x/web-interface/nav")
        if not data.get("isLogin"):
            raise PlatformError("B 站尚未登录或登录已过期", code="auth_expired", pause=True)
        uid = str(data.get("mid", ""))
        if self.expected_uid and uid != self.expected_uid:
            raise PlatformError("实际登录账号与配置的机器人 UID 不一致", code="wrong_account", pause=True)
        if self.credentials.get("DedeUserID") and uid != self.credentials["DedeUserID"]:
            raise PlatformError("登录凭证中的 UID 不一致", code="wrong_account", pause=True)
        self._account = {"uid": uid, "name": str(data.get("uname", ""))}
        return self._account

    def _signed(self, params):
        if not self._wbi_key or self._wbi_key[1] < time.time():
            nav, _ = self._request("/x/web-interface/nav")
            wbi = nav.get("wbi_img") or {}
            original = "".join(urlparse(wbi[key]).path.rsplit("/", 1)[-1].split(".")[0] for key in ("img_url", "sub_url"))
            if len(original) < 64:
                raise PlatformError("无法取得站内接口签名参数")
            self._wbi_key = ("".join(original[i] for i in MIXIN_INDEX)[:32], time.time() + 1800)
        params["wts"] = int(time.time())
        cleaned = {str(k): re.sub(r"[!'()*]", "", str(v)) for k, v in sorted(params.items())}
        cleaned["w_rid"] = md5((urlencode(cleaned) + self._wbi_key[0]).encode()).hexdigest()
        return cleaned

    def generate_login(self) -> dict:
        data, _ = self._request(PASSPORT + "/x/passport-login/web/qrcode/generate")
        return {"url": data["url"], "key": data["qrcode_key"]}

    def poll_login(self, key: str) -> str:
        data, response = self._request(PASSPORT + "/x/passport-login/web/qrcode/poll", params={"qrcode_key": key})
        status = int(data.get("code", -1))
        if status == 0:
            credentials = {cookie.name: cookie.value for cookie in response.cookies.jar
                           if cookie.name in {"SESSDATA", "bili_jct", "DedeUserID", "buvid3", "buvid4"}}
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
    def notification_event(item: dict, kind: str, account_id: str, now: float) -> Event | None:
        source, user = item.get("item") or {}, item.get("user") or {}
        rpid = str(source.get("source_id") or "")
        oid = str(source.get("subject_id") or "")
        business = int(source.get("business_id") or 0)
        if not rpid.isdigit() or not oid.isdigit() or business not in TYPE_KINDS:
            return None
        actor = str(user.get("mid") or "")
        if not actor.isdigit() or actor == account_id:
            return None
        ref = ContentRef("bilibili", TYPE_KINDS[business], oid)
        root = str(source.get("root_id") or rpid)
        return Event(f"bilibili:{account_id}:comment:{business}:{oid}:{rpid}", "bilibili", account_id, actor,
                     f"bilibili:{account_id}:{ref.key}:thread:{root}", kind,
                     plain(source.get("source_content", "")),
                     float(item.get("at_time") or item.get("reply_time") or now),
                     content=ref, reply_to=rpid, thread_root=root, actor_name=plain(user.get("nickname")))

    def notifications(self, now: float) -> list[Event]:
        uid = (self._account or self.account())["uid"]
        events = {}
        for endpoint, kind in (("/x/msgfeed/reply", "reply"), ("/x/msgfeed/at", "mention")):
            params = {"ps": 30, "platform": "web"}
            for _ in range(self.pages):
                data, _ = self._request(endpoint, params=params)
                items = data.get("items") or []
                for item in items:
                    event = self.notification_event(item, kind, uid, now)
                    if event and event.occurred_at >= now - self.max_age:
                        events[event.event_id] = event
                cursor = data.get("cursor") or {}
                if not items or cursor.get("is_end", True):
                    break
                params.update({"id": cursor.get("id", items[-1].get("id", 0)),
                               "reply_time" if kind == "reply" else "at_time": cursor.get("time", 0)})
            else:
                # Do not silently pretend an unbounded backlog has been fully consumed.
                raise PlatformError("通知超过单轮分页上限，请增加 notification_pages 或先处理积压", code="notification_backlog")
        return sorted(events.values(), key=lambda event: event.occurred_at)

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
        uid = (self._account or self.account())["uid"]
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
            data, _ = self._request("/x/v2/reply/reply", params={"oid": numeric(ref.external_id), "type": type_, "root": numeric(root or reply_to), "ps": 20, "pn": 1})
            rows = ([data["root"]] if data.get("root") else []) + (data.get("replies") or [])
        else:
            data, _ = self._request("/x/v2/reply/main", params={"oid": numeric(ref.external_id), "type": type_, "mode": 3, "ps": 5})
            rows = data.get("replies") or []
        for row in rows:
            comments.append({"id": str(row.get("rpid_str") or row.get("rpid", "")),
                             "root": str(row.get("root_str") or row.get("root") or row.get("rpid", "")),
                             "text": str((row.get("content") or {}).get("message", ""))[:1500]})
        return tuple(comments)

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

    def read(self, ref: ContentRef, *, reply_to="", root="") -> Document:
        if ref.provider != "bilibili" or ref.kind not in set(COMMENT_TYPES) | {"post"}:
            raise PlatformError("不支持的内容引用")
        oid = numeric(ref.external_id)
        if ref.kind == "video":
            data, _ = self._request("/x/web-interface/view", params={"aid": oid})
            bvid = str(data.get("bvid", ""))
            transcripts = []
            for page in (data.get("pages") or [{"cid": data.get("cid")}])[:3]:
                cid = str(page.get("cid") or "")
                if cid.isdigit():
                    text = self._subtitle(bvid, cid)
                    if text:
                        transcripts.append(str(page.get("part") or "") + "\n" + text)
            body = "\n".join(transcripts)[:20000]
            level = "transcript" if body else "metadata"
            body = body or str(data.get("desc") or "")
            title, author, category = plain(data.get("title")), str((data.get("owner") or {}).get("mid", "")), str(data.get("tid", ""))
            url = "https://www.bilibili.com/video/" + bvid
        else:
            if ref.kind in {"dynamic", "post"}:
                data, _ = self._request("/x/polymer/web-dynamic/v1/detail", params={"id": oid})
                item = data.get("item") or {}
                dynamic = (item.get("modules") or {}).get("module_dynamic") or {}
                opus = (dynamic.get("major") or {}).get("opus") or {}
                body = str((dynamic.get("desc") or {}).get("text") or (opus.get("summary") or {}).get("text") or "")
                if ref.kind == "post" and not body:
                    archive = (dynamic.get("major") or {}).get("archive") or {}
                    body = str(archive.get("title") or "")
                author = str(((item.get("modules") or {}).get("module_author") or {}).get("mid", ""))
            elif ref.kind == "draw":
                data, _ = self._request("https://api.bilibili.com/x/dynamic/feed/draw/doc_detail", params={"doc_id": oid})
                body = str((data.get("item") or {}).get("description") or "")
                author = str((data.get("user") or {}).get("uid", ""))
            else:
                raise PlatformError("专栏通知可识别，正文读取尚不支持", code="unsupported_content")
            title, category, url = "动态", "", "https://www.bilibili.com/opus/" + oid if ref.kind in {"dynamic", "post"} else ""
            level = "text" if body else "metadata"
        comments = self._comments(ref, reply_to, root) if ref.kind != "post" else ()
        return Document(ref, title, body[:20000], level, author, category, url, comments)

    def discover(self, category_ids: Sequence[str], limit: int) -> list[ContentRef]:
        refs = {}
        for category in category_ids:
            data, _ = self._request("/x/web-interface/newlist", params={"rid": numeric(category), "pn": 1, "ps": min(limit, 30)})
            for row in data.get("archives") or []:
                aid = str(row.get("aid") or "")
                if aid.isdigit():
                    ref = ContentRef("bilibili", "video", aid)
                    refs[ref.key] = ref
        return list(refs.values())[:limit]

    def search(self, query: str, category_ids: Sequence[str], limit: int) -> list[ContentRef]:
        refs = {}
        for category in list(category_ids) or ["0"]:
            data, _ = self._request("/x/web-interface/wbi/search/type", signed=True, params={
                "search_type": "video", "keyword": query[:120], "tids": numeric(category), "page": 1,
                "page_size": min(limit, 20), "order": "totalrank",
            })
            for row in data.get("result") or []:
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
        if action.kind in {"owner_repost", "artwork_share"}:
            return self._send_dynamic(action, before_write=before_write)
        if action.content is None or action.content.kind not in COMMENT_TYPES:
            raise PlatformError("B 站适配器不支持此发送目标")
        names = {self._mention_name(uid): int(uid) for uid in mentions.values()}
        message = "".join(f"@{name} " for name in names) + action.text
        fields = {"oid": numeric(action.content.external_id), "type": COMMENT_TYPES[action.content.kind],
                  "message": message, "csrf": self.credentials["bili_jct"], "plat": 1}
        if action.reply_to:
            fields.update({"root": numeric(action.thread_root or action.reply_to), "parent": numeric(action.reply_to)})
        if names:
            fields["at_name_to_mid"] = json.dumps(names, ensure_ascii=False)
        if before_write is not None and not before_write():
            return Receipt("failed", detail="发送前的最终检查未通过：任务、绑定或运行状态已经改变")
        try:
            response = self.client.post(API + "/x/v2/reply/add", data=fields)
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
        return Receipt("accepted", rpid, "平台已受理，公开可见性尚未独立验证", mention_resolved)

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

    def _write_json(self, path: str, *, data=None, json_body=None, files=None) -> tuple[dict, Receipt | None]:
        try:
            response = self.client.post(API + path, params={"csrf": self.credentials["bili_jct"]},
                                        data=data, json=json_body, files=files)
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
            raise PlatformError(f"动态操作未受理，接口错误 {code}", code=str(code), pause=code in {-101, -111, -403, -412, -509, -352, -401, 12015})
        return payload.get("data") or {}, None

    def _send_dynamic(self, action: Action, *, before_write=None) -> Receipt:
        def allowed():
            return before_write is None or before_write()
        request = {"content": {"contents": [{"raw_text": action.text, "type": 1, "biz_id": ""}]},
                   "scene": 1, "meta": {"app_meta": {"from": "create.dynamic.web", "mobi_app": "web"}},
                   "upload_id": f"{action.account_id}_{int(action.created_at)}_{int(md5(action.action_id.encode()).hexdigest()[:8], 16) % 10000:04d}"}
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
