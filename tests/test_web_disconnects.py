from http.server import BaseHTTPRequestHandler
from io import BytesIO
from types import SimpleNamespace

import pytest

import murasamebot.bootstrap.web as web
from test_architecture_and_config import write_config


class DisconnectingWriter:
    def __init__(self, error, fail_at):
        self.error, self.fail_at, self.calls = error, fail_at, 0
        self.written = bytearray()

    def write(self, value):
        self.calls += 1
        if self.calls >= self.fail_at:
            raise self.error("simulated local disconnect")
        self.written.extend(value)


def request(path, *, method="GET", url="/api/data/events", writer=None, body=b""):
    cls = web.handler_for(path, "test-token")
    handler = object.__new__(cls)
    handler.path, handler.command = url, method
    handler.request_version, handler.requestline = "HTTP/1.1", method + " " + url + " HTTP/1.1"
    handler.headers = {"Host": "127.0.0.1", "Authorization": "Bearer test-token", "Content-Length": str(len(body))}
    handler.wfile = writer if writer is not None else BytesIO()
    handler.rfile = BytesIO(body)
    handler.close_connection = False
    return handler


@pytest.mark.parametrize("error", web.CLIENT_DISCONNECTS)
@pytest.mark.parametrize("fail_at", [1, 2])
def test_disconnected_get_does_not_send_a_second_400_response(tmp_path, monkeypatch, error, fail_at):
    path = write_config(tmp_path)
    reads = []
    monkeypatch.setattr(web, "prepare_store", lambda cfg: SimpleNamespace(inspect=lambda table: reads.append(table) or []))
    writer = DisconnectingWriter(error, fail_at)
    handler = request(path, writer=writer)
    handler.do_GET()
    assert reads == ["events"]
    assert writer.calls == fail_at
    assert handler.close_connection
    assert handler.response(400, {"error": "should never be written"}) is False
    assert writer.calls == fail_at


def test_post_committed_before_disconnect_is_not_replayed(tmp_path, monkeypatch):
    path = write_config(tmp_path)
    writes = []
    monkeypatch.setattr(web, "prepare_store", lambda cfg: SimpleNamespace(set_setting=lambda key, value: writes.append((key, value))))
    writer = DisconnectingWriter(ConnectionAbortedError, 2)
    handler = request(path, method="POST", url="/api/pause", writer=writer, body=b"{}")
    handler.do_POST()
    assert writes == [("paused", True)]
    assert writer.calls == 2
    assert handler.close_connection


def test_real_data_error_still_returns_400_and_another_request_remains_usable(tmp_path, monkeypatch):
    path = write_config(tmp_path)
    def broken_store(cfg):
        raise OSError("local file unavailable")
    monkeypatch.setattr(web, "prepare_store", broken_store)
    failed = request(path)
    failed.do_GET()
    assert b"400 Bad Request" in failed.wfile.getvalue()
    assert b"local file unavailable" in failed.wfile.getvalue()
    monkeypatch.setattr(web, "prepare_store", lambda cfg: SimpleNamespace(inspect=lambda table: []))
    working = request(path)
    working.do_GET()
    assert b"200 OK" in working.wfile.getvalue()


@pytest.mark.parametrize("error", web.CLIENT_DISCONNECTS)
def test_disconnect_while_reading_or_flushing_only_ends_that_handler(tmp_path, monkeypatch, error):
    handler = request(write_config(tmp_path))
    def disconnected(self):
        raise error("simulated read/flush disconnect")
    monkeypatch.setattr(BaseHTTPRequestHandler, "handle", disconnected)
    handler.handle()
    assert handler.close_connection
    assert handler.response(400, {}) is False
