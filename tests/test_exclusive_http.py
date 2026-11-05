from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
from threading import Thread

import httpx
import pytest

from murasamebot.bootstrap import qq_receiver, web
from murasamebot.bootstrap.http_server import ExclusiveHTTPServer


def test_dashboard_and_callback_both_use_exclusive_listener():
    assert web.ThreadingHTTPServer is ExclusiveHTTPServer
    assert qq_receiver.ThreadingHTTPServer is ExclusiveHTTPServer


def test_second_http_server_cannot_share_the_listener_even_with_reuseaddr():
    first=ExclusiveHTTPServer(('127.0.0.1',0),BaseHTTPRequestHandler)
    address=first.server_address
    try:
        with pytest.raises(OSError):
            second=ThreadingHTTPServer(address,BaseHTTPRequestHandler)
            second.server_close()
        with socket.socket() as challenger:
            challenger.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            with pytest.raises(OSError):challenger.bind(address)
    finally:first.server_close()
    # Closing the original listener releases it; stale files are irrelevant.
    replacement=ExclusiveHTTPServer(address,BaseHTTPRequestHandler)
    replacement.server_close()


def test_exclusive_listener_restarts_immediately_after_real_loopback_request():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200);self.send_header('Content-Length','2');self.end_headers();self.wfile.write(b'ok')
        def log_message(self,*args):pass
    first=ExclusiveHTTPServer(('127.0.0.1',0),Handler)
    address=first.server_address
    thread=Thread(target=first.serve_forever,daemon=True);thread.start()
    try:
        response=httpx.get(f'http://127.0.0.1:{address[1]}/',trust_env=False,timeout=3)
        assert response.status_code==200 and response.text=='ok'
    finally:
        first.shutdown();first.server_close();thread.join(3)
    replacement=ExclusiveHTTPServer(address,Handler)
    replacement.server_close()
