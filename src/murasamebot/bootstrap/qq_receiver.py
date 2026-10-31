"""Authenticated OneBot callback; only durable ingestion, no model/sends here."""
import hashlib
import hmac
import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from murasamebot.bootstrap.config import Config
from murasamebot.bootstrap.runtime import prepare_store
from murasamebot.adapters.persistence.chat_batches import ChatBatches
from murasamebot.adapters.platforms.qq import QQAdapter


class BodyError(ValueError):
    def __init__(self, status):
        self.status = status


def read_body(headers, stream, limit=262144):
    """Read bounded HTTP framing; NapCat's Node client streams chunked JSON."""
    transfer = headers.get("Transfer-Encoding", "").strip().lower()
    length = headers.get("Content-Length")
    if transfer:
        if transfer != "chunked" or length is not None:
            raise BodyError(400)
        body = bytearray()
        for _ in range(4096):
            line = stream.readline(128)
            size_text = line.partition(b";")[0].strip()
            if not line.endswith(b"\r\n") or not re.fullmatch(rb"[0-9a-fA-F]+", size_text):
                raise BodyError(400)
            size = int(size_text, 16)
            if len(body) + size > limit:
                raise BodyError(413)
            if size == 0:
                trailer_size = 0
                while True:
                    trailer = stream.readline(1024)
                    trailer_size += len(trailer)
                    if not trailer.endswith(b"\r\n") or trailer_size > 8192:
                        raise BodyError(400)
                    if trailer == b"\r\n":
                        if not body:
                            raise BodyError(400)
                        return bytes(body)
            chunk = stream.read(size)
            if len(chunk) != size or stream.read(2) != b"\r\n":
                raise BodyError(400)
            body.extend(chunk)
        raise BodyError(413)
    if length is None:
        raise BodyError(411)
    if not re.fullmatch(r"[0-9]+", length):
        raise BodyError(400)
    size = int(length)
    if not 0 < size <= limit:
        raise BodyError(413)
    body = stream.read(size)
    if len(body) != size:
        raise BodyError(400)
    return body


def handler_for(path):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Never log private messages, signed URLs or callback secrets.

        def do_POST(self):
            adapter = None
            try:
                config = Config(path)
                if self.path != "/onebot/qq" or "qq" not in config.enabled_platforms:
                    self.send_error(404); return
                if getattr(self, "connection", None):
                    self.connection.settimeout(15)
                body = read_body(self.headers, self.rfile)
                secret = config.data["qq"]["callback_secret"]
                expected = "sha1="+hmac.new(secret.encode(),body,hashlib.sha1).hexdigest()
                if not secret or not hmac.compare_digest(self.headers.get("X-Signature", ""),expected):
                    self.send_error(401); return
                payload = json.loads(body)
                if not isinstance(payload,dict):
                    raise ValueError("invalid event")
                store = prepare_store(config)
                adapter = QQAdapter(config.data["qq"],store,config.data["owner_person_id"])
                adapter.group_settings=config.data['qq_groups']
                event = adapter.parse(payload)
                if event:
                    binding = store.binding(config.data["owner_person_id"],"qq")
                    if binding:
                        ChatBatches(store).receive(event,binding,time.time())
                self.send_response(200)
                self.send_header("Content-Type","application/json")
                self.send_header("Content-Length","2")
                self.end_headers()
                self.wfile.write(b"{}")
            except BodyError as exc:
                self.send_error(exc.status)
            except Exception:
                # Report no raw exceptions from HTTP clients or received JSON.
                self.send_error(503,"QQ event was not committed; retry later")
            finally:
                if adapter:
                    adapter.close()
    return Handler


def create_server(path):
    config = Config(path)
    if "qq" not in config.enabled_platforms or not config.data["qq"]["callback_secret"]:
        raise ValueError("请先配置并启用QQ模块及回调密钥")
    server = ThreadingHTTPServer(("127.0.0.1",config.data["qq"]["callback_port"]),handler_for(path))
    server.daemon_threads = True
    return server


def serve(path):
    server = create_server(path)
    try:
        server.serve_forever()
    finally:
        server.server_close()
