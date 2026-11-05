"""Prevent multiple local dashboards/receivers sharing a Windows listener."""
from http.server import ThreadingHTTPServer
import os
import socket


class ExclusiveHTTPServer(ThreadingHTTPServer):
    # POSIX SO_REUSEADDR supports normal restart without sharing listeners.
    # Windows SO_REUSEADDR may allow a second server to bind the same address.
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()
