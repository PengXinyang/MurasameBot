"""Shared outbound routing. Local administration never uses this factory."""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
import ipaddress
from pathlib import Path

import httpx

from murasamebot.adapters.proxy_rules import RuleCache


GFWLIST_URL = "https://raw.githubusercontent.com/gfwlist/gfwlist/master/gfwlist.txt"
DIRECT_DOMAINS = ("bilibili.com", "b23.tv", "biliapi.com", "biliapi.net", "bilivideo.com",
                  "hdslb.com", "biliimg.com", "qq.com", "wechat.com", "weixin.com", "qpic.cn",
                  "qlogo.cn", "gtimg.com", "gtimg.cn")


@dataclass(frozen=True)
class ProxyPolicy:
    proxy_url: str = field(repr=False)
    cache_file: Path
    mode: str = "gfwlist"
    source_url: str = GFWLIST_URL
    refresh_hours: int = 6
    direct_domains: tuple[str, ...] = DIRECT_DOMAINS

    def bypass(self, host: str) -> bool:
        host = host.lower().rstrip(".")
        if host == "localhost" or host.endswith(".localhost"):
            return True
        try:
            if not ipaddress.ip_address(host).is_global:
                return True
        except ValueError:
            pass
        return any(host == domain or host.endswith("." + domain) for domain in self.direct_domains)

    def should_proxy(self, url: httpx.URL | str) -> bool:
        url = httpx.URL(url)
        if not self.proxy_url or self.bypass(url.host):
            return False
        return self.mode == "all" or rules_for(self).get().matches(str(url.copy_with(userinfo=b"")))


# One cache/refresh lock per effective policy, shared by independent model lanes.
@lru_cache(maxsize=16)
def rules_for(policy: ProxyPolicy) -> RuleCache:
    return RuleCache(policy.cache_file, policy.source_url, policy.proxy_url, policy.refresh_hours)


class RoutingTransport(httpx.BaseTransport):
    def __init__(self, policy: ProxyPolicy, *, direct=None, proxied=None):
        self.policy = policy
        self.direct = direct if direct is not None else httpx.HTTPTransport(trust_env=False)
        self.proxied = proxied if proxied is not None else httpx.HTTPTransport(proxy=policy.proxy_url, trust_env=False)

    def handle_request(self, request):
        # Redirects followed by the caller are matched again. Never silently
        # retry a failed selected route using the other route.
        transport = self.proxied if self.policy.should_proxy(request.url) else self.direct
        return transport.handle_request(request)

    def close(self):
        try:
            self.direct.close()
        finally:
            self.proxied.close()


def client(*, proxy_url: str | ProxyPolicy = "", **kwargs) -> httpx.Client:
    if isinstance(proxy_url, ProxyPolicy) and proxy_url.proxy_url:
        return httpx.Client(transport=RoutingTransport(proxy_url), trust_env=False, follow_redirects=False, **kwargs)
    # Strings retain compatibility for independently used adapters/tests.
    # Application composition always supplies the policy rather than a raw URL.
    return httpx.Client(proxy=(proxy_url if isinstance(proxy_url, str) else "") or None,
                        trust_env=False, follow_redirects=False, **kwargs)
