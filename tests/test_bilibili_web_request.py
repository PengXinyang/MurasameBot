"""Protocol checks use public example keys, never captured user credentials."""
from dataclasses import replace
from hashlib import md5
from urllib.parse import parse_qs

import httpx
import pytest

from test_bili_cli_compatibility import adapter, comment_action
from test_bilibili_contracts import nav_data
from murasamebot.adapters.platforms.bilibili import PlatformError
from murasamebot.domain.models import ContentRef


def test_wbi_matches_published_example_and_does_not_mutate_input(tmp_path, monkeypatch):
    monkeypatch.setattr("murasamebot.adapters.platforms.bilibili.time.time", lambda: 1702204169)
    bot = adapter(tmp_path, lambda request: httpx.Response(200, json={"code": 0, "data": nav_data()}))
    source = {"foo": "114", "bar": "514", "zab": 1919810, "w_rid": "obsolete"}
    signed = bot._signed(source)
    assert signed["w_rid"] == "8f6f2b5b3d485fe1886cec6a0be8c5d4"
    assert source["w_rid"] == "obsolete" and "wts" not in source


def test_wbi_uses_uri_encoding_for_spaces_chinese_and_filter_characters(tmp_path, monkeypatch):
    monkeypatch.setattr("murasamebot.adapters.platforms.bilibili.time.time", lambda: 1702204169)
    bot = adapter(tmp_path, lambda request: httpx.Response(200, json={"code": 0, "data": nav_data()}))
    signed = bot._signed({"foo": "one one four!'()*", "bar": "五一四"})
    # Independently specified canonical query, rather than the implementation's
    # encoder: spaces must be %20, not the HTML form '+' encoding.
    canonical = "bar=%E4%BA%94%E4%B8%80%E5%9B%9B&foo=one%20one%20four&wts=1702204169"
    assert signed["w_rid"] == md5((canonical + "ea1db124af3c7062474693fa704f4ff8").encode()).hexdigest()
    assert signed["foo"] == "one one four"


def test_each_video_write_has_fresh_signature_and_reuses_its_account_nav(tmp_path, monkeypatch):
    stamp = [1702204169]
    monkeypatch.setattr("murasamebot.adapters.platforms.bilibili.time.time", lambda: stamp[0])
    requests = []
    def handle(request):
        requests.append(request)
        if request.url.path.endswith("/nav"):
            return httpx.Response(200, json={"code": 0, "data": nav_data()})
        return httpx.Response(200, json={"code": 0, "data": {"rpid": 800}})
    bot = adapter(tmp_path, handle)
    assert bot.send(comment_action(), {}).state == "accepted"
    stamp[0] += 60
    assert bot.send(comment_action(), {}).state == "accepted"
    assert [r.method for r in requests] == ["GET", "POST", "GET", "POST"]
    first, second = requests[1], requests[3]
    assert first.url.params["wts"] != second.url.params["wts"]
    assert first.url.params["w_rid"] != second.url.params["w_rid"]
    for request in (first, second):
        assert set(request.url.params) == {"w_rid", "wts"}
        assert "csrf" not in request.url.params
        assert not any(name.startswith("dm_") or name == "b_wet" for name in request.url.params)
        assert request.headers["origin"] == "https://www.bilibili.com"
        assert request.headers["referer"] == "https://www.bilibili.com/video/av42/"
        assert request.headers["content-type"] == "application/x-www-form-urlencoded"
        assert parse_qs(request.content.decode())["csrf"] == ["csrf"]


@pytest.mark.parametrize("wbi", [None, [], {}, {"img_url": "https://example.invalid/not-a-key.png", "sub_url": ""}])
def test_missing_or_malformed_keys_never_fall_back_to_unsigned_post(tmp_path, wbi):
    calls = []
    def handle(request):
        calls.append(request)
        assert request.method == "GET"
        return httpx.Response(200, json={"code": 0, "data": {"isLogin": True, "mid": 999, "wbi_img": wbi}})
    with pytest.raises(PlatformError, match="签名参数"):
        adapter(tmp_path, handle).send(comment_action(), {})
    assert all(r.method == "GET" for r in calls)


def test_final_gate_runs_after_signature_refresh_and_still_blocks_all_writes(tmp_path):
    calls = []
    def handle(request):
        calls.append(request)
        assert request.method == "GET"
        data = {"isLogin": True, "mid": 999} if len(calls) == 1 else nav_data()
        return httpx.Response(200, json={"code": 0, "data": data})
    gates = []
    def gate():
        gates.append(len(calls))
        return False
    receipt = adapter(tmp_path, handle).send(comment_action(), {}, before_write=gate)
    assert receipt.state == "failed" and gates == [2]


def test_existing_draw_comment_policy_does_not_require_video_signing(tmp_path):
    writes = []
    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json={"code": 0, "data": {"isLogin": True, "mid": 999}})
        writes.append(request)
        return httpx.Response(200, json={"code": 0, "data": {"rpid": 800}})
    bot = adapter(tmp_path, handle)
    receipt = bot.send(replace(comment_action(), content=ContentRef("bilibili", "draw", "42")), {})
    assert receipt.state == "accepted" and len(writes) == 1
    assert not writes[0].url.query and writes[0].headers["origin"] == "https://www.bilibili.com"


def test_signature_expiry_refreshes_key_instead_of_reusing_old_key(tmp_path, monkeypatch):
    monkeypatch.setattr("murasamebot.adapters.platforms.bilibili.time.time", lambda: 1702204169)
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={"code": 0, "data": nav_data()})
    bot = adapter(tmp_path, handle)
    bot._wbi_key = ("outdated", 1702204168)
    signed = bot._signed({})
    assert len(calls) == 1 and bot._wbi_key[0] == "ea1db124af3c7062474693fa704f4ff8"
    assert signed["w_rid"] == md5(b"wts=1702204169ea1db124af3c7062474693fa704f4ff8").hexdigest()
