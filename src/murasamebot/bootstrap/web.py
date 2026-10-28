from __future__ import annotations

import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import secrets
import sqlite3
from threading import Event, Thread
import time
from urllib.parse import urlparse

from murasamebot.bootstrap.config import Config
from murasamebot.bootstrap.runtime import build, prepare_store, readiness
from murasamebot.domain.models import PlatformError


CHARACTER_ASSETS = {
    "/assets/character/normal.png": "正常.png",
    "/assets/character/happy.png": "高兴.png",
}
CLIENT_DISCONNECTS = (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)


def admin_token(root: Path) -> str:
    path = root / ".secrets" / "admin-token"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(secrets.token_urlsafe(32), encoding="ascii")
        path.chmod(0o600)
    return path.read_text(encoding="ascii").strip()


def handler_for(config_path: Path, token: str):
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
                self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
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
            if path == "/assets/dashboard.css" or path in CHARACTER_ASSETS:
                if self.headers.get("Host", "").split(":")[0] not in {"127.0.0.1", "localhost"}:
                    self.response(401, {"error": "仅限本机访问"})
                    return
                try:
                    if path == "/assets/dashboard.css":
                        asset = Path(__file__).with_name("dashboard.css")
                        self.response(200, asset.read_bytes(), content_type="text/css; charset=utf-8")
                    else:
                        # Explicit public UI assets only: never expose the character
                        # directory, configuration, arbitrary filenames or secrets.
                        config = Config(config_path)
                        asset = config.file("character_source").parent / "avatar" / CHARACTER_ASSETS[path]
                        body = asset.read_bytes()
                        if len(body) > 10_000_000 or not body.startswith(b"\x89PNG\r\n\x1a\n"):
                            raise ValueError("无效的界面图片")
                        self.response(200, body, content_type="image/png")
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
                elif path == "/api/status":
                    runtime = build(config)
                    try:
                        self.response(200, readiness(runtime))
                    finally:
                        runtime.close()
                elif path == "/api/memory/scopes":
                    self.response(200,prepare_store(config).memory_scopes())
                elif path.startswith("/api/data/"):
                    table = path.rsplit("/", 1)[-1]
                    self.response(200, prepare_store(config).inspect(table))
                else:
                    self.response(404, {"error": "不存在的接口"})
            except (ValueError, OSError, KeyError) as exc:
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
                if path == "/api/config":
                    config.save_local(data)
                    if "paused" in data:
                        store.set_setting("paused", bool(data["paused"]))
                elif path == "/api/emojis/refresh":
                    runtime = build(config)
                    try:
                        uid = runtime.platform.account()["uid"]
                        options = runtime.platform.emoji_options(uid, "public")
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
                elif path == "/api/resume":
                    store.set_setting("paused", False)
                    store.set_setting("platform_paused:bilibili", False)
                elif path == "/api/approve":
                    store.approve_action(str(data["id"]), time.time())
                elif path == "/api/reject":
                    store.reject_action(str(data["id"]))
                elif path == "/api/publication/check":
                    from dataclasses import asdict
                    from murasamebot.application.publication import PublicationService
                    runtime = build(config)
                    try:
                        result = PublicationService(store, {"bilibili": runtime.platform}).check(str(data["id"]))
                        self.response(200, asdict(result))
                    finally:
                        runtime.close()
                    return
                elif path == "/api/cancel":
                    store.cancel_task(str(data["id"]))
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
    config = Config(config_path)
    token = admin_token(config.root)
    host, port = config.data["web"]["host"], config.data["web"]["port"]
    server = ThreadingHTTPServer((host, port), handler_for(config_path, token))
    stop = Event()
    worker = None
    if with_worker:
        from murasamebot.bootstrap.cli import worker_loop
        def work():
            try:
                worker_loop(config_path, stop)
            except (ValueError, OSError, KeyError) as exc:
                logging.getLogger("murasame").error("工作循环未启动：%s", exc)
        worker = Thread(target=work, daemon=True, name="murasame-worker")
        worker.start()
    print(f"本机管理台：http://{host}:{port}/#token={token}")
    print("该链接可管理本地账号，请保存在本机。" + ("工作循环已请求启动。" if with_worker else "当前仅运行管理台。"))
    try:
        server.serve_forever(poll_interval=0.3)
    finally:
        stop.set()
        server.server_close()
        if worker:
            worker.join(timeout=2)
