import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import os
import time

import httpx
import pytest
import yaml

from murasamebot.adapters.network import ProxyPolicy, RoutingTransport, client, rules_for
from murasamebot.adapters.proxy_rules import RuleCache, RuleSet
from murasamebot.bootstrap.config import Config
from test_architecture_and_config import write_config


RULES = b"""[AutoProxy 0.2.9]
! A representative URL list, deliberately including default-direct services.
||pixiv.net^
||pximg.net^
||twitter.com^
||x.com^
||bilibili.com^
||qq.com^
||wechat.com^
@@||safe.pixiv.net^
|https://special.example/art/*|
/^https?:\\/\\/[^\\/]+blogspot\\.(.*)/
@@/^https?:\\/\\/allowed\\.blogspot\\.example$
"""


def policy(tmp_path):
    path = tmp_path / "rules.txt"
    path.write_bytes(base64.b64encode(RULES))
    return ProxyPolicy("http://127.0.0.1:7890", path)


@pytest.mark.parametrize("encoded", [False, True])
def test_url_rules_include_paths_regex_and_exception_precedence(encoded):
    rules = RuleSet(base64.b64encode(RULES) if encoded else RULES)
    for url in ["https://www.pixiv.net/artworks/123", "https://i.pximg.net/a.jpg", "http://x.com/", "https://a.blogspot.example/a", "https://special.example/art/12"]:
        assert rules.matches(url), url
    for url in ["https://safe.pixiv.net/art", "https://notpixiv.net/a", "https://pixiv.net.evil/a", "https://api.deepseek.com/", "http://special.example/art/12", "https://allowed.blogspot.example"]:
        assert not rules.matches(url), url


def test_direct_service_and_lan_bypass_does_not_need_a_rule_download(tmp_path, monkeypatch):
    p = policy(tmp_path)
    p.cache_file.unlink()
    monkeypatch.setattr(RuleCache, "_download", lambda self: pytest.fail("direct traffic must not load rules"))
    for host in ["api.bilibili.com", "b23.tv", "i0.hdslb.com", "s1.biliimg.com", "im.qq.com", "weixin.qq.com", "wechat.com", "wx.qlogo.cn", "qpic.cn", "gtimg.com", "localhost", "127.0.0.1", "192.168.0.1", "10.0.0.4", "[::1]"]:
        assert not p.should_proxy("https://" + host + "/"), host
    assert not p.bypass("qq.com.evil.example")
    assert not p.bypass("fakebilibili.com")


def test_routing_chooses_both_paths_and_ignores_environment_proxies(tmp_path, monkeypatch):
    p = policy(tmp_path)
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    seen = []
    def recorder(route):
        def handle(request):
            seen.append((route, request.url.host))
            return httpx.Response(200, text=route)
        return httpx.MockTransport(handle)
    transport = RoutingTransport(p, direct=recorder("direct"), proxied=recorder("proxy"))
    with httpx.Client(transport=transport, trust_env=False) as http:
        for host in ["api.bilibili.com", "api.deepseek.com", "www.pixiv.net", "safe.pixiv.net", "x.com"]:
            http.get("https://" + host)
    assert seen == [("direct", "api.bilibili.com"), ("direct", "api.deepseek.com"),
                    ("proxy", "www.pixiv.net"), ("direct", "safe.pixiv.net"), ("proxy", "x.com")]


def test_proxy_failure_never_retries_direct_and_redirect_is_rematched(tmp_path):
    p = policy(tmp_path)
    seen = []
    def direct(request):
        seen.append("direct")
        return httpx.Response(302, headers={"Location": "https://www.pixiv.net/art"})
    def proxy(request):
        seen.append("proxy")
        raise httpx.ConnectError("unavailable", request=request)
    with httpx.Client(transport=RoutingTransport(p, direct=httpx.MockTransport(direct), proxied=httpx.MockTransport(proxy)), follow_redirects=True) as http:
        with pytest.raises(httpx.ConnectError):
            http.get("https://example.com/start")
    assert seen == ["direct", "proxy"]


def test_disabled_and_optional_all_mode_still_keep_local_and_platform_direct(tmp_path, monkeypatch):
    p = policy(tmp_path)
    monkeypatch.setattr(RuleCache, "_download", lambda self: pytest.fail("must not fetch rules"))
    assert not replace(p, proxy_url="").should_proxy("https://www.pixiv.net")
    assert replace(p, mode="all").should_proxy("https://api.deepseek.com")
    assert not replace(p, mode="all").should_proxy("https://api.bilibili.com")
    with client(proxy_url=p) as http:
        assert isinstance(http._transport, RoutingTransport)


def test_cache_download_is_shared_by_parallel_clients_and_survives_restart(tmp_path, monkeypatch):
    p = policy(tmp_path)
    p.cache_file.unlink()
    downloads = []
    def download(self):
        downloads.append(True)
        time.sleep(0.01)
        return base64.b64encode(RULES)
    monkeypatch.setattr(RuleCache, "_download", download)
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert all(pool.map(lambda _: p.should_proxy("https://www.pixiv.net"), range(16)))
    assert len(downloads) == 1
    assert RuleCache(p.cache_file, p.source_url, p.proxy_url, 6).get().matches("https://x.com")
    assert len(downloads) == 1


def test_failed_refresh_keeps_last_good_file_and_uses_retry_backoff(tmp_path, monkeypatch):
    p = policy(tmp_path)
    original = p.cache_file.read_bytes()
    os.utime(p.cache_file, (1, 1))
    attempts = []
    def fail(self):
        attempts.append(True)
        return b"<html>not a list</html>"
    monkeypatch.setattr(RuleCache, "_download", fail)
    assert p.should_proxy("https://x.com")
    assert p.should_proxy("https://www.pixiv.net")
    assert len(attempts) == 1
    assert p.cache_file.read_bytes() == original
    assert "上次有效规则" in rules_for(p).status()["warning"]


def test_no_cache_and_failed_download_does_not_guess_routes(tmp_path, monkeypatch):
    p = policy(tmp_path)
    p.cache_file.unlink()
    monkeypatch.setattr(RuleCache, "_download", lambda self: b"broken")
    with pytest.raises(httpx.ConnectError, match="代理规则不可用"):
        p.should_proxy("https://api.deepseek.com")
    assert not p.should_proxy("https://api.bilibili.com")
    assert not p.cache_file.exists()


def test_corrupt_fresh_cache_is_repaired_even_when_a_previous_version_is_loaded(tmp_path, monkeypatch):
    p = policy(tmp_path)
    assert p.should_proxy("https://x.com")
    old_stat = p.cache_file.stat()
    p.cache_file.write_bytes(b"corrupt")
    # A deterministic version of Windows' same-timestamp fast-write case.
    os.utime(p.cache_file, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
    monkeypatch.setattr(RuleCache, "_download", lambda self: RULES)
    assert p.should_proxy("https://x.com")
    assert p.cache_file.read_bytes() == RULES


@pytest.mark.parametrize("content", [b"garbage", b"[AutoProxy 0.2.9]\n! empty", b"[AutoProxy 0.2.9]\n||a.com$script", b"[AutoProxy 0.2.9]\n/[/"])
def test_invalid_or_unsupported_rules_are_rejected(content):
    with pytest.raises(ValueError):
        RuleSet(content)


def test_config_defaults_validation_and_dashboard_save_preserve_private_proxy(tmp_path):
    path = write_config(tmp_path)
    config = Config(path)
    assert config.network_proxy.mode == "gfwlist"
    assert config.network_proxy.bypass("im.qq.com")
    config.save_local({"network": {"proxy_mode": "gfwlist", "gfwlist_refresh_hours": 12,
                                   "direct_domains": ["bilibili.com", "qq.com", "wechat.com"]}})
    assert Config(path).network_proxy.refresh_hours == 12
    for patch in [{"proxy_mode": "invalid"}, {"gfwlist_refresh_hours": 0}, {"direct_domains": ["https://qq.com"]}, {"gfwlist_url": "http://rules.example/a"}]:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        data["network"].update(patch)
        with pytest.raises(ValueError):
            Config.validate(data)


def test_runtime_wires_policy_into_platform_text_vision_and_artwork_clients(tmp_path):
    from murasamebot.bootstrap.runtime import build
    path = write_config(tmp_path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["network"]["proxy_enabled"] = True
    data["vision"]["base_url"] = "https://vision.example"
    data["artwork"]["sources"] = ["pixiv", "x"]
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    runtime = build(Config(path))
    try:
        clients = [runtime.platform.client, runtime.model.client]
        clients += [resource.client for resource in runtime.resources]
        clients += [source.client for source in runtime.artwork.sources.values()]
        assert len(clients) == 7  # Separate GET-only owner-history client.
        assert runtime.owner_history.source.client is not runtime.platform.client
        assert all(isinstance(c._transport, RoutingTransport) and c._transport.policy.mode == "gfwlist" for c in clients)
    finally:
        runtime.close()


def test_route_cli_does_not_open_database_or_connect_to_targets(tmp_path, monkeypatch, capsys):
    from murasamebot.bootstrap import cli
    path = write_config(tmp_path)
    config = Config(path)
    monkeypatch.setattr(cli, "prepare_store", lambda *_: pytest.fail("no database access"))
    monkeypatch.setattr(cli, "build", lambda *_: pytest.fail("no account clients"))
    cli.main(["--config", str(path), "proxy-rules", "https://api.bilibili.com"])
    assert '"route": "direct"' in capsys.readouterr().out
    assert not config.file("database").exists()
