from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socketserver
from threading import Thread

import httpx
import pytest
import yaml

from murasamebot.adapters.network import client
from murasamebot.bootstrap.config import Config
from test_architecture_and_config import ROOT, write_config


@contextmanager
def running(server):
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


def test_http_proxy_carries_http_and_https_connect_without_direct_fallback():
    seen = []
    class Proxy(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.command, self.path))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'proxy-only')

        def do_CONNECT(self):
            seen.append((self.command, self.path))
            self.send_response(502)  # Test the CONNECT request without an external TLS target.
            self.end_headers()

        def log_message(self, *args):
            pass
    with running(ThreadingHTTPServer(("127.0.0.1", 0), Proxy)) as port:
        with client(proxy_url=f"http://127.0.0.1:{port}", timeout=2) as http:
            assert http.get("http://unresolvable.invalid/art").text == "proxy-only"
            with pytest.raises(httpx.ProxyError):
                http.get("https://unresolvable.invalid/art")
    assert seen == [("GET", "http://unresolvable.invalid/art"), ("CONNECT", "unresolvable.invalid:443")]


@pytest.mark.parametrize("scheme", ["socks5", "socks5h"])
def test_socks_proxy_negotiates_domain_and_carries_http(scheme):
    seen = []
    class Socks(socketserver.StreamRequestHandler):
        def handle(self):
            header = self.rfile.read(2)
            assert header[0] == 5
            methods = self.rfile.read(header[1])
            assert 0 in methods
            self.wfile.write(b"\x05\x00")
            request = self.rfile.read(4)
            assert request == b"\x05\x01\x00\x03"
            domain = self.rfile.read(self.rfile.read(1)[0]).decode()
            port = int.from_bytes(self.rfile.read(2), "big")
            seen.append((domain, port))
            self.wfile.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x50")
            line = self.rfile.readline()
            assert line.startswith(b"GET /art ")
            while self.rfile.readline() not in (b"\r\n", b""):
                pass
            self.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\nConnection: close\r\n\r\nsocks-only")
    with running(socketserver.ThreadingTCPServer(("127.0.0.1", 0), Socks)) as port:
        with client(proxy_url=f"{scheme}://127.0.0.1:{port}", timeout=2) as http:
            assert http.get("http://unresolvable.invalid/art").text == "socks-only"
    assert seen == [("unresolvable.invalid", 80)]


def test_public_template_has_no_owner_identity_and_unset_uid_is_not_bound(tmp_path, monkeypatch):
    monkeypatch.delenv("MURASAME_OWNER_UID", raising=False)
    data = yaml.safe_load((ROOT / "config.yml").read_text(encoding="utf-8"))
    assert data["bindings"][0]["external_id"] == "${MURASAME_OWNER_UID}"
    path = tmp_path / "config.yml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert Config(path).data["bindings"] == []
    monkeypatch.setenv("MURASAME_OWNER_UID", "987654")
    assert Config(path).data["bindings"][0]["external_id"] == "987654"


def test_secret_sessions_tokens_and_proxy_password_are_redacted(tmp_path, monkeypatch):
    path = write_config(tmp_path)
    monkeypatch.setenv("MURASAME_PIXIV_SESSION", "private-session")
    monkeypatch.setenv("MURASAME_X_BEARER_TOKEN", "private-api-token")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["network"]["proxy_url"] = "http://user:private-proxy-password@127.0.0.1:7890"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    public = str(Config(path).public())
    assert all(secret not in public for secret in ("private-session", "private-api-token", "private-proxy-password"))
    config = Config(path)
    config.save_local({"network": {"proxy_url": config.public()["network"]["proxy_url"], "proxy_enabled": True}})
    assert Config(path).proxy_url == "http://user:private-proxy-password@127.0.0.1:7890"


def test_environment_expansion_cannot_bypass_proxy_validation(tmp_path, monkeypatch):
    path = write_config(tmp_path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["network"]["proxy_url"] = "${TEST_PROXY}"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.setenv("TEST_PROXY", "file:///private")
    with pytest.raises(ValueError):
        Config(path)
