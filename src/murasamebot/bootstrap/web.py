from __future__ import annotations

import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import sqlite3
import time
from urllib.parse import urlparse, parse_qs

from murasamebot.bootstrap.config import Config
from murasamebot.bootstrap.runtime import build, prepare_store, readiness
from murasamebot.bootstrap.owner_history import OwnerLoginManager, history_status, history_source, history_service, optional_history
from murasamebot.application.request_policy import RequestSettings
from murasamebot.domain.models import PlatformError


CHARACTER_ASSETS = {
    "/assets/character/normal.png": "正常.png",
    "/assets/character/happy.png": "高兴.png",
    "/assets/character/dashboard-smile.jpg": "dashboard-smile.jpg",
    "/assets/character/dashboard-letter.jpg": "dashboard-letter.jpg",
    "/assets/character/dashboard-kimono.png": "dashboard-kimono.png",
}
CLIENT_DISCONNECTS = (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)


def admin_token(root: Path) -> str:
    path = root / ".secrets" / "admin-token"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(secrets.token_urlsafe(32), encoding="ascii")
        path.chmod(0o600)
    return path.read_text(encoding="ascii").strip()


def handler_for(config_path: Path, token: str, *, receivers=None):
    owner_login = OwnerLoginManager()
    class Handler(BaseHTTPRequestHandler):
        def handle(self):
            try:
                super().handle()
            except CLIENT_DISCONNECTS:
                # Covers a closed browser while reading a request or flushing,
                # as well as response()'s normal header/body write protection.
                self._client_disconnected = True
                self.close_connection = True

        def log_message(self, format, *args):
            # Avoid recording tokens, private requests or URL query parameters.
            pass

        def response(self, status, data, *, html=False, content_type=None):
            if getattr(self, "_client_disconnected", False):
                return False
            body = data if isinstance(data, bytes) else data.encode("utf-8") if html else json.dumps(data, ensure_ascii=False).encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", content_type or ("text/html; charset=utf-8" if html else "application/json; charset=utf-8"))
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' blob:; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
                self.end_headers()
                self.wfile.write(body)
                return True
            except CLIENT_DISCONNECTS:
                # A peer disconnect is not a configuration/database error. Never
                # send another HTTP error to this socket or rerun a POST action.
                self._client_disconnected = True
                self.close_connection = True
                return False

        def authorized(self):
            host = self.headers.get("Host", "")
            if host.split(":")[0] not in {"127.0.0.1", "localhost"}:
                return False
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).netloc != host:
                return False
            return hmac.compare_digest(self.headers.get("Authorization", "").encode("utf-8"), ("Bearer " + token).encode("utf-8"))

        def do_GET(self):
            path = urlparse(self.path).path
            console_assets = {"/assets/"+name for name in ("console-common.js","bilibili-console.js","qq-console.js","shared-memory.js","receivers.js")}
            if path in console_assets or path in {"/assets/dashboard.css","/assets/owner-history.js","/assets/request-controls.js","/assets/social-state.js"} or path in CHARACTER_ASSETS:
                if self.headers.get("Host", "").split(":")[0] not in {"127.0.0.1", "localhost"}:
                    self.response(401, {"error": "仅限本机访问"})
                    return
                try:
                    if path in console_assets:
                        self.response(200,Path(__file__).with_name(path.rsplit('/',1)[-1]).read_bytes(),content_type="text/javascript; charset=utf-8")
                    elif path == "/assets/social-state.js":
                        self.response(200,Path(__file__).with_name("social-state.js").read_bytes(),content_type="text/javascript; charset=utf-8")
                    elif path == "/assets/request-controls.js":
                        self.response(200,Path(__file__).with_name("request-controls.js").read_bytes(),content_type="text/javascript; charset=utf-8")
                    elif path == "/assets/owner-history.js":
                        self.response(200,Path(__file__).with_name("owner-history.js").read_bytes(),content_type="text/javascript; charset=utf-8")
                    elif path == "/assets/dashboard.css":
                        asset = Path(__file__).with_name("dashboard.css")
                        self.response(200, asset.read_bytes(), content_type="text/css; charset=utf-8")
                    else:
                        # Explicit public UI assets only: never expose the character
                        # directory, configuration, arbitrary filenames or secrets.
                        config = Config(config_path)
                        asset = config.file("character_source").parent / "avatar" / CHARACTER_ASSETS[path]
                        body = asset.read_bytes()
                        is_png = asset.suffix == ".png" and body.startswith(b"\x89PNG\r\n\x1a\n")
                        is_jpeg = asset.suffix == ".jpg" and body.startswith(b"\xff\xd8\xff")
                        if len(body) > 10_000_000 or not (is_png or is_jpeg):
                            raise ValueError("无效的界面图片")
                        self.response(200, body, content_type="image/png" if is_png else "image/jpeg")
                except (OSError, ValueError, KeyError):
                    self.response(404, {"error": "界面素材不可用"})
                return
            if path == "/":
                self.response(200, Path(__file__).with_name("dashboard.html").read_text(encoding="utf-8"), html=True)
                return
            if not self.authorized():
                self.response(401, {"error": "需要本机管理令牌"})
                return
            try:
                config = Config(config_path)
                if path == "/api/config":
                    self.response(200, config.public())
                elif path == '/api/receivers':
                    self.response(200,receivers.status() if receivers else {"managed":False,"platforms":{}})
                elif path.startswith('/api/platforms/'):
                    from murasamebot.bootstrap.console import platform_console
                    self.response(200,platform_console(config,path.rsplit('/',1)[-1]))
                elif path == "/api/requests":
                    self.response(200,{"settings":config.data["requests"],"private_poll_seconds":config.data["bilibili"]["private_poll_seconds"],
                        "status":prepare_store(config).request_status(time.time(),RequestSettings(**config.data["requests"]))})
                elif path == "/api/owner-history":
                    self.response(200, history_status(config,prepare_store(config)))
                elif path == "/api/owner-history/qr":
                    self.response(200, owner_login.image(),content_type="image/png")
                elif path == "/api/status":
                    runtime = build(config)
                    try:
                        self.response(200, readiness(runtime))
                    finally:
                        runtime.close()
                elif path == "/api/social":
                    store = prepare_store(config)
                    from murasamebot.application.social import SocialService, SocialSettings
                    with optional_history(config,store) as observations:
                        SocialService(store,SocialSettings(**config.data["social"]),owner_person_id=config.data["owner_person_id"],
                                      observations=observations).context(channel="dynamic",now=time.time())
                    records = []
                    for row in store.social_rows(time.time(),include_inactive=True):
                        source = {k:v for k,v in row["source"].items() if k not in {"activity_anchor","owner_activity_version"}}
                        records.append({k:row[k] for k in ("state_id","status","version","created_at","expires_at","next_at","followups","public_allowed")}|{"source":source,"state":row["state"]})
                    self.response(200,{"settings":config.data["social"],"records":records})
                elif path == "/api/memory/scopes":
                    self.response(200,prepare_store(config).memory_scopes())
                elif path == "/api/memory/journals":
                    store=prepare_store(config)
                    platform = parse_qs(urlparse(self.path).query).get('platform',[''])[0]
                    if platform not in {'','qq','bilibili'}:
                        raise ValueError('未知记忆来源平台')
                    feeds={kind:store.memory_feed(kind,platform=platform) for kind in ("memory","diary","reflection")}
                    with optional_history(config,store) as observations:
                        if observations is None or not observations.diary_authorized():
                            feeds["diary"]=[p for p in feeds["diary"] if p["purpose"]!="owner_observation_diary"]
                    self.response(200,feeds)
                elif path.startswith("/api/data/"):
                    table = path.rsplit("/", 1)[-1]
                    self.response(200, prepare_store(config).inspect(table))
                else:
                    self.response(404, {"error": "不存在的接口"})
            except (ValueError, OSError, KeyError, PlatformError) as exc:
                self.response(400, {"error": str(exc)})
            except sqlite3.Error:
                self.response(503, {"error": "本地数据暂忙，请稍后刷新"})

        def do_POST(self):
            if not self.authorized():
                # Drain a small rejected body before closing. On Windows, an
                # unread POST body can reset the socket and hide the 401 reply.
                try:
                    rejected_length = int(self.headers.get("Content-Length","0"))
                    if 0<rejected_length<=65536:
                        self.connection.settimeout(1)
                        self.rfile.read(rejected_length)
                except (ValueError,OSError):
                    pass
                self.response(401, {"error": "需要本机管理令牌"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 65536:
                    raise ValueError("请求长度无效")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError("请求必须是对象")
                config = Config(config_path)
                store = prepare_store(config)
                path = urlparse(self.path).path
                if path=='/api/qq-calls/control':
                    if receivers is None:raise ValueError('请通过管理台的统一接收服务启动通话')
                    self.response(200,receivers.call_control(data));return
                if path in {'/api/qq-learning/manage','/api/qq-learning/sources'}:
                    from murasamebot.adapters.persistence.group_learning import GroupKnowledge
                    room=str(data.get('room',''))
                    if room not in config.data['qq_groups']['group_ids']:raise ValueError('群不在授权范围')
                    repo=GroupKnowledge(store);account=config.data['qq']['robot_id']
                    if path.endswith('/sources'):
                        self.response(200,repo.sources(account,room,str(data['id']),time.time()));return
                    repo.manage(account,room,str(data['id']),str(data['operation']),data.get('meaning'))
                elif path == "/api/receivers":
                    if receivers is None:
                        raise ValueError("当前服务未装配ReceiverManager，请通过serve启动管理台")
                    if set(data) != {"platform", "operation"} or not all(isinstance(v,str) for v in data.values()):
                        raise ValueError("只接受平台名称和start/stop操作")
                    self.response(200,receivers.control(data["platform"],data["operation"]))
                    return
                elif path == "/api/qq/check":
                    from murasamebot.bootstrap.console import check_qq
                    self.response(200,check_qq(config))
                elif path == "/api/owner-history/login/start":
                    self.response(200,owner_login.start(config,store))
                    return
                elif path == "/api/owner-history/login/poll":
                    self.response(200,owner_login.poll(config,store))
                    return
                elif path == "/api/owner-history/revoke":
                    owner_login.revoke(config,store)
                elif path == "/api/owner-history/clear":
                    store.owner_history_reset(config.data["owner_person_id"],time.time())
                elif path == "/api/owner-history/sync":
                    source = history_source(config,store)
                    try:
                        history_service(config,store,source).sync(time.time(),force=True)
                        self.response(200,history_status(config,store))
                        return
                    finally:
                        source.close()
                elif path == "/api/config":
                    if receivers:
                        receivers.configure(data)
                    else:
                        config.save_local(data)
                    if "paused" in data:
                        store.set_setting("paused", bool(data["paused"]))
                elif path == "/api/emojis/refresh":
                    runtime = build(config)
                    try:
                        platform = runtime.require_platform("bilibili")
                        uid = platform.account()["uid"]
                        options = platform.emoji_options(uid, "public")
                        from dataclasses import asdict
                        from murasamebot.domain.models import stable_id
                        key = f"emoji_catalog:bilibili:{uid}:public:{stable_id(runtime.engine.expressions.settings.preferred_keywords)}"
                        store.set_setting(key, {"items": [asdict(e) for e in options], "expires_at": time.time()+config.data.get("expressions",{}).get("cache_minutes",360)*60})
                        self.response(200, {"ok": True, "count": len(options), "preferred": [e.token for e in options if any(k in e.token for k in config.data.get("expressions",{}).get("preferred_keywords",["千恋万花","丛雨"]))]})
                        return
                    finally:
                        runtime.close()
                elif path == "/api/pause":
                    store.set_setting("paused", True)
                    store.audit("platform_paused","管理台手动暂停全部工作",time.time())
                elif path == "/api/resume":
                    store.set_setting("paused", False)
                    for platform_name in config.enabled_platforms:
                        store.set_setting(f"platform_paused:{platform_name}", False)
                    store.audit("platform_resumed","管理台手动恢复工作；接口冷却仍按各自期限执行",time.time())
                elif path == "/api/approve":
                    store.approve_action(str(data["id"]), time.time())
                elif path == "/api/actions/send":
                    if set(data) != {"id"} or not isinstance(data["id"],str):
                        raise ValueError("只接受消息ID，不能通过发送接口修改正文或收件人")
                    runtime = build(config)
                    try:
                        action = runtime.engine.queue_manual_send(data["id"])
                        self.response(200,{"ok":True,"state":"queued","id":action.action_id,
                                           "detail":"已加入发送队列；请保持工作循环运行，额度与发送间隔仍生效"})
                    finally:
                        runtime.close()
                    return
                elif path == "/api/reject":
                    store.reject_action(str(data["id"]))
                elif path == "/api/publication/check":
                    from dataclasses import asdict
                    from murasamebot.application.publication import PublicationService
                    runtime = build(config)
                    try:
                        result = PublicationService(store, {name: module.adapter for name, module in runtime.modules.items()}).check(str(data["id"]))
                        self.response(200, asdict(result))
                    finally:
                        runtime.close()
                    return
                elif path == "/api/cancel":
                    store.cancel_task(str(data["id"]))
                elif path == "/api/social/manage":
                    if data.get("operation") not in {"resolve","forget"}:
                        raise ValueError("未知的社交状态操作")
                    store.social_forget(str(data["id"]),resolve=data["operation"]=="resolve")
                    store.audit("social_managed",data["operation"]+":"+str(data["id"]),time.time())
                elif path == "/api/forget":
                    store.forget(str(data["id"]))
                elif path == "/api/memory/search":
                    self.response(200,store.memory_records(str(data["person_id"]),str(data["scope"]),str(data["purpose"]),query=str(data.get("query","")),limit=100))
                    return
                elif path == "/api/memory/sources":
                    self.response(200,store.memory_product_source_records(str(data["product_id"])) if "product_id" in data else store.memory_source_records(data["ids"]))
                    return
                elif path == "/api/memory/edit":
                    store.memory_edit(str(data["id"]),text=data.get("text"),pinned=data.get("pinned"),forget=data.get("forget") is True,now=time.time())
                elif path == "/api/memory/forget-source":
                    store.forget_source(str(data["id"]))
                elif path == "/api/memory/rebuild":
                    store.memory_rebuild(str(data["person_id"]),str(data["scope"]),str(data["purpose"]))
                elif path == "/api/memory/retry":
                    store.set_setting("memory_retry_at",0)
                else:
                    self.response(404, {"error": "不存在的接口"})
                    return
                self.response(200, {"ok": True})
            except (ValueError, OSError, KeyError, PlatformError) as exc:
                self.response(400, {"error": str(exc)})
            except sqlite3.Error:
                self.response(503, {"error": "本地数据暂忙，请稍后重试"})
    return Handler


def serve(config_path: Path, *, with_worker=False):
    from murasamebot.bootstrap.receivers import ReceiverManager
    config = Config(config_path)
    token = admin_token(config.root)
    host, port = config.data["web"]["host"], config.data["web"]["port"]
    receivers = ReceiverManager(config_path)
    server = ThreadingHTTPServer((host, port), handler_for(config_path, token, receivers=receivers))
    server.daemon_threads = True
    print(f"本机管理台：http://{host}:{port}/#token={token}")
    print("该链接可管理本地账号，请保存在本机。" + ("已请求启动配置中的平台服务。" if with_worker else "可在运行概况中分别启动QQ与B站。"))
    try:
        if with_worker:
            receivers.start_configured()
        server.serve_forever(poll_interval=0.3)
    finally:
        receivers.close()
        server.server_close()
