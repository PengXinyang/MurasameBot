"""One explicit outbound proxy policy; loopback admin traffic never uses this factory."""
from __future__ import annotations

import httpx


def client(*, proxy_url: str = "", **kwargs) -> httpx.Client:
    # Environment proxies must not override an explicit disabled setting. A broken
    # configured proxy fails visibly; there is no silent direct-network fallback.
    return httpx.Client(proxy=proxy_url or None, trust_env=False, follow_redirects=False, **kwargs)
