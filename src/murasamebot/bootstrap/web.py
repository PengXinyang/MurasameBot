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


def admin_token(root: Path) -> str:
    path = root / ".secrets" / "admin-token"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(secrets.token_urlsafe(32), encoding="ascii")
        path.chmod(0o600)
    return path.read_text(encoding="ascii").strip()


def handler_for(config_path: Path, token: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            # Avoid recording tokens, private requests or URL query parameters.
            pass

        def response(self, status, data, *, html=False):
            body = data.encode("utf-8") if html else json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8" if html else "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

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
                elif path == "/api/pause":
                    store.set_setting("paused", True)
                elif path == "/api/resume":
                    store.set_setting("paused", False)
                    store.set_setting("platform_paused:bilibili", False)
                elif path == "/api/approve":
                    store.approve_action(str(data["id"]), time.time())
                elif path == "/api/reject":
                    store.reject_action(str(data["id"]))
                elif path == "/api/cancel":
                    store.cancel_task(str(data["id"]))
                elif path == "/api/forget":
                    store.forget(str(data["id"]))
                else:
                    self.response(404, {"error": "不存在的接口"})
                    return
                self.response(200, {"ok": True})
            except (ValueError, OSError, KeyError) as exc:
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
