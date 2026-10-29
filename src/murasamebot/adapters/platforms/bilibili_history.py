"""Separate owner credentials and a strictly GET-only endpoint allowlist."""
import json
import math
import logging
import re
from pathlib import Path
from uuid import uuid4
from urllib.parse import urlparse

import httpx

from murasamebot.adapters.network import client as network_client
from murasamebot.domain.models import PlatformError, stable_id


NAV = "https://api.bilibili.com/x/web-interface/nav"
HISTORY = "https://api.bilibili.com/x/web-interface/history/cursor"
QR_START = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QR_POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
# The API still lives on passport, but now also issues account-host QR pages.
QR_PAGE_HOSTS = {"passport.bilibili.com", "account.bilibili.com"}


class _HideQR(logging.Filter):
    def filter(self, record):
        return "/qrcode/" not in record.getMessage()


logging.getLogger("httpx").addFilter(_HideQR())


class BilibiliOwnerHistory:
    def __init__(self, path: Path, *, proxy_url="", client=None, monitor=None):
        self.monitor=monitor
        self.path = path
        self.client = client or network_client(proxy_url=proxy_url, monitor=monitor, timeout=httpx.Timeout(20,connect=8),
            headers={"User-Agent":"Mozilla/5.0", "Referer":"https://www.bilibili.com/"})

    def close(self):
        self.client.close()

    def _get(self, url, *, params=None):
        if url not in {NAV,HISTORY,QR_START,QR_POLL}:
            raise PlatformError("主人观察客户端拒绝非白名单接口", code="owner_endpoint_denied")
        try:
            response = self.client.get(url,params=params)
            if response.status_code != 200:
                raise PlatformError("主人观察接口暂不可用", code="owner_http_error")
            body = response.json()
            if not isinstance(body,dict) or body.get("code") != 0 or not isinstance(body.get("data"),dict):
                raise PlatformError("主人登录失效或接口不可用", code="owner_auth_or_api_error")
            return body["data"],response
        except (httpx.HTTPError, ValueError):
            raise PlatformError("主人观察连接或响应异常",code="owner_network_error") from None

    def _load(self, binding):
        if not binding or not self.path.exists():
            raise PlatformError("请先用绑定的主人账号扫码",code="owner_login_required")
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data,dict):
            raise PlatformError("主人登录文件无效，请重新扫码",code="owner_login_invalid")
        if (data.get("uid") != str(binding["external_id"]) or data.get("person_id") != binding["person_id"]
                or data.get("binding_version") != binding["version"] or not data.get("authorization_id")
                or not isinstance(data.get("cookies"),dict) or not isinstance(data["cookies"].get("SESSDATA"),str)
                or not data["cookies"].get("SESSDATA")):
            raise PlatformError("主人绑定已变化，请重新扫码",code="owner_identity_mismatch")
        return data

    def authorization(self, binding):
        return self._load(binding)["authorization_id"]

    def _identity(self, expected):
        data,_ = self._get(NAV)
        if not data.get("isLogin") or str(data.get("mid")) != str(expected):
            raise PlatformError("扫码账号与绑定的主人不一致，未保存登录",code="owner_identity_mismatch")

    def start_login(self):
        self.client.cookies.clear()
        data,_ = self._get(QR_START)
        url, key = data.get("url"), data.get("qrcode_key")
        valid = False
        if (isinstance(url,str) and 0<len(url)<=4096 and url==url.strip()
                and not re.search(r"[\x00-\x20\x7f\\]",url)
                and isinstance(key,str) and 0<len(key)<=256 and not re.search(r"\s",key)):
            try:
                parsed = urlparse(url)
                valid = (parsed.scheme == "https" and parsed.hostname in QR_PAGE_HOSTS
                         and parsed.port in {None,443} and parsed.username is None and parsed.password is None)
            except ValueError:
                pass
        if not valid:
            raise PlatformError("B站返回的扫码地址不受支持，请更新程序或重试",code="owner_qr_invalid")
        return url, key

    def poll_login(self, key, binding):
        if not binding:
            raise PlatformError("请先配置主人绑定",code="owner_binding_missing")
        data,response = self._get(QR_POLL,params={"qrcode_key":key})
        status = data.get("code")
        if status != 0:
            return {86101:"waiting",86090:"scanned",86038:"expired"}.get(status,"error")
        cookies = {}
        for cookie in response.cookies.jar:
            if cookie.domain.lstrip(".") in {"bilibili.com","passport.bilibili.com","api.bilibili.com"} and cookie.name in {"SESSDATA","DedeUserID"}:
                cookies[cookie.name] = cookie.value
        if not cookies.get("SESSDATA"):
            raise PlatformError("扫码未返回主人登录凭证",code="owner_login_incomplete")
        self.client.cookies.clear()
        self.client.cookies.set("SESSDATA",cookies["SESSDATA"],domain="api.bilibili.com",path="/")
        self._identity(binding["external_id"])
        self.path.parent.mkdir(parents=True,exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps({"uid":str(binding["external_id"]),"person_id":binding["person_id"],
            "binding_version":binding["version"],"authorization_id":uuid4().hex,"cookies":cookies}),encoding="utf-8")
        temp.chmod(0o600)
        temp.replace(self.path)
        return "success"

    def read_history(self, binding, *, pages, since):
        data = self._load(binding)
        self.client.cookies.clear()
        self.client.cookies.set("SESSDATA",data["cookies"]["SESSDATA"],domain="api.bilibili.com",path="/")
        self._identity(binding["external_id"])
        cursor,seen,rows = {},set(),{}
        warning = ""
        for _ in range(max(1,min(3,pages))):
            page,_ = self._get(HISTORY,params={"ps":30,"type":"archive",**cursor})
            batch = page.get("list")
            if not isinstance(batch,list):
                raise PlatformError("观看历史格式无效",code="owner_history_format")
            if not batch:
                break
            times = []
            for item in batch[:30]:
                if not isinstance(item,dict):
                    continue
                h = item.get("history") or {}
                oid = str(h.get("oid") or "")
                at = item.get("view_at")
                if h.get("business") != "archive" or not re.fullmatch(r"\d{1,24}",oid) or type(at) not in {int,float} or not math.isfinite(at):
                    continue
                times.append(at)
                numeric = lambda v: int(v) if type(v) in {int,float} and math.isfinite(v) else 0
                rows[stable_id(oid,at)] = {"content_id":oid,"title":str(item.get("title") or "")[:500],
                    "author_name":str(item.get("author_name") or "")[:100],"category":str(item.get("tag_name") or "")[:100],
                    "viewed_at":at,"progress":numeric(item.get("progress")),"duration":numeric(item.get("duration")),
                    "url":"https://www.bilibili.com/video/av"+oid}
            if times and min(times)<=since or len(batch)<30:
                break
            value = page.get("cursor") or {}
            next_cursor = {k:value.get(k, "" if k=="business" else 0) for k in ("max","view_at","business")}
            fingerprint = stable_id(next_cursor)
            if fingerprint in seen or not next_cursor["max"]:
                warning = "历史分页未能继续，覆盖可能不完整"
                break
            seen.add(fingerprint)
            cursor = next_cursor
        else:
            warning = "已达到历史读取页数上限，较旧记录不保证补回"
        return list(rows.values()),warning
