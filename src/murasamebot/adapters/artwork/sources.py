from __future__ import annotations

from html import unescape
import re
from urllib.parse import quote

import httpx

from murasamebot.adapters.network import client as network_client
from murasamebot.domain.models import Artwork, ContentRef, PlatformError


def clean(value) -> str:
    return unescape(re.sub(r"<[^>]*>", " ", str(value or "")))[:3000]


class ArtHTTP:
    source = ""

    def __init__(self, *, proxy_url="", client: httpx.Client | None = None):
        self.client = client or network_client(proxy_url=proxy_url, timeout=25, headers={"User-Agent": "Mozilla/5.0"})

    def get(self, url, **kwargs) -> dict:
        try:
            response = self.client.get(url, **kwargs)
            if response.status_code != 200:
                raise PlatformError(f"美图来源 HTTP {response.status_code}；请检查登录、接口权限或代理", platform=self.source)
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError()
            return data
        except (httpx.HTTPError, ValueError):
            raise PlatformError("美图来源连接或解析失败；未绕过登录和访问限制", platform=self.source) from None

    def close(self):
        self.client.close()


class PixivSource(ArtHTTP):
    """Read-only web AJAX endpoints; optional PHPSESSID, no captcha bypass."""
    source = "pixiv"

    def __init__(self, session="", **kwargs):
        super().__init__(**kwargs)
        if session:
            self.client.cookies.set("PHPSESSID", session, domain="www.pixiv.net", path="/")
        self.client.headers["Referer"] = "https://www.pixiv.net/"

    def body(self, path, **kwargs):
        data = self.get("https://www.pixiv.net" + path, **kwargs)
        if data.get("error") or not isinstance(data.get("body"), dict):
            raise PlatformError("Pixiv 返回错误或登录失效；请更新本地会话", platform=self.source)
        return data["body"]

    def search_artworks(self, keyword: str, limit: int) -> list[Artwork]:
        body = self.body("/ajax/search/illustrations/" + quote(keyword, safe=""), params={
            "word": keyword, "mode": "safe", "type": "illust", "s_mode": "s_tag", "order": "date_d", "p": 1,
        })
        result = []
        for row in (body.get("illust") or {}).get("data", [])[:limit]:
            wid = str(row.get("id", ""))
            if not wid.isdigit() or row.get("isAdContainer"):
                continue
            detail = self.body("/ajax/illust/" + wid)
            if detail.get("illustType") != 0:
                continue
            tags = tuple(clean(t.get("tag")) for t in (detail.get("tags") or {}).get("tags", []))
            result.append(Artwork(ContentRef("pixiv", "artwork", wid), clean(detail.get("illustTitle") or detail.get("title")),
                                  str(detail.get("userId") or ""), clean(detail.get("userName")),
                                  "https://www.pixiv.net/artworks/" + wid, str((detail.get("urls") or {}).get("original") or ""),
                                  tags, clean(detail.get("description")),
                                  safe=type(detail.get("xRestrict")) is int and detail["xRestrict"] == 0,
                                  is_ai=detail.get("aiType") == 2))
        return result


class XSource(ArtHTTP):
    """Official X API; access/usage charges depend on the operator's API plan."""
    source = "x"

    def __init__(self, bearer_token="", **kwargs):
        super().__init__(**kwargs)
        self.bearer_token = bearer_token

    def search_artworks(self, keyword: str, limit: int) -> list[Artwork]:
        if not self.bearer_token:
            raise PlatformError("X 搜索需要在本地配置官方 API Bearer Token", platform="x")
        # Do not treat configured keywords as arbitrary API query operators.
        term = re.sub(r'["():\\\n\r]', " ", keyword)
        body = self.get("https://api.x.com/2/tweets/search/recent", headers={"Authorization": "Bearer " + self.bearer_token}, params={
            "query": f'"{term}" has:images -is:retweet', "max_results": max(10, min(limit, 100)),
            "expansions": "author_id,attachments.media_keys", "tweet.fields": "possibly_sensitive,author_id,attachments",
            "user.fields": "username,name", "media.fields": "type,url",
        })
        if body.get("errors") and not body.get("data"):
            raise PlatformError("X 搜索权限或查询失败；请检查官方 API 配额", platform="x")
        includes = body.get("includes") or {}
        users = {str(u["id"]): u for u in includes.get("users", [])}
        media = {m["media_key"]: m for m in includes.get("media", [])}
        result = []
        for post in body.get("data", [])[:limit]:
            uid, pid = str(post.get("author_id", "")), str(post.get("id", ""))
            user = users.get(uid, {})
            pictures = [media.get(k, {}) for k in (post.get("attachments") or {}).get("media_keys", [])]
            picture = next((p for p in pictures if p.get("type") == "photo" and p.get("url")), None)
            if not picture or not uid.isdigit() or not pid.isdigit() or not user.get("username"):
                continue
            text = clean(post.get("text"))
            result.append(Artwork(ContentRef("x", "artwork", pid), text[:150], uid, clean(user.get("name")),
                                  f"https://x.com/{user['username']}/status/{pid}", picture["url"],
                                  tuple(re.findall(r"#([^\s#]+)", text)), text,
                                  safe=post.get("possibly_sensitive") is False,
                                  is_ai=bool(re.search(r"AI.?art|AI.?イラスト|AI生成", text, re.I))))
        return result
