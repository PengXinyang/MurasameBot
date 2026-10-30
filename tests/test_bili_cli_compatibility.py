from dataclasses import replace
import json
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest

from murasamebot.adapters.platforms.bilibili import BilibiliAdapter, PlatformError
from murasamebot.application.publication import PublicationService
from murasamebot.domain.models import Action, ContentRef, PublicationCheck, Receipt
from test_bilibili_contracts import credentials


def comment_action():
    return Action("a", "agent", "bilibili", "999", ContentRef("bilibili", "video", "42"),
                  "唔，这个思路倒挺巧。", 1000, 2000, kind="reply", reply_to="700", thread_root="600")


def adapter(tmp_path, handle):
    return BilibiliAdapter(credentials(tmp_path), client=httpx.Client(transport=httpx.MockTransport(handle)))


def nav():
    from test_bilibili_contracts import nav_data
    return httpx.Response(200, json={"code": 0, "data": nav_data()})


@pytest.mark.parametrize("state,status", [(17, "self_only"), (0, "unverified")])
def test_publish_uses_web_metadata_and_distinguishes_receipt_from_readback(tmp_path, state, status):
    posts = []
    def handle(request):
        if request.url.path.endswith("/nav"):
            return nav()
        assert request.method == "POST" and request.url.path == "/x/v2/reply/add"
        posts.append(request)
        fields = parse_qs(request.content.decode())
        assert fields["gaia_source"] == ["main_web"]
        assert json.loads(fields["statistics"][0]) == {"appId": 100, "platform": 5}
        assert fields["root"] == ["600"] and fields["parent"] == ["700"]
        assert fields["message"] == [comment_action().text]
        assert "AppleWebKit" in request.headers["user-agent"]
        assert request.headers["referer"] == "https://www.bilibili.com/video/av42/"
        assert request.headers["origin"] == "https://www.bilibili.com"
        assert fields["at_name_to_mid"] == ["{}"]
        assert set(request.url.params) == {"w_rid", "wts"}
        assert "SESSDATA=test-secret" in request.headers["cookie"]
        return httpx.Response(200, json={"code": 0, "data": {"reply": {"rpid_str": "800", "state": state}}})
    bot = adapter(tmp_path, handle)
    receipt = bot.send(comment_action(), {})
    assert receipt.state == "accepted" and receipt.external_id == "800"
    assert receipt.publication_check.status == status
    assert len(posts) == 1


@pytest.mark.parametrize("state,extra,expected", [(0, {}, "account_visible"), (17, {"invisible": False}, "self_only"),
    (11, {}, "pending"), (5, {}, "abnormal"), (0, {"folder": {"is_folded": True}}, "folded"),
    (0, {"invisible": True}, "abnormal"), (None, {}, "unverified")])
def test_read_check_does_not_equate_self_view_with_public_visibility(tmp_path, state, extra, expected):
    calls = []
    def handle(request):
        assert request.method == "GET"
        calls.append(request.url.path)
        if request.url.path.endswith("/nav"):
            return nav()
        assert request.url.params["root"] == "600"
        return httpx.Response(200, json={"code": 0, "data": {"page": {"count": 1}, "replies": [
            {"rpid": 800, "oid": 42, "mid": 999, "state": state, **extra}]}})
    result = adapter(tmp_path, handle).check_publication(comment_action(), "800")
    assert result.status == expected and result.platform_state == state
    assert result.checked_pages == 1 and len(calls) == 2
    if expected == "account_visible":
        assert "其他账号" in result.detail


def test_read_check_stops_after_two_pages_and_does_not_call_missing_deleted(tmp_path):
    pages = []
    def handle(request):
        if request.url.path.endswith("/nav"):
            return nav()
        assert request.method == "GET"
        pages.append(request.url.params["pn"])
        return httpx.Response(200, json={"code": 0, "data": {"page": {"count": 200}, "replies": [{"rpid": 1}]}})
    result = adapter(tmp_path, handle).check_publication(comment_action(), "800")
    assert result.status == "not_found" and pages == ["1", "2"]
    assert "可能" in result.detail


def test_read_check_finds_second_page_and_root_comments(tmp_path):
    def handle(request):
        if request.url.path.endswith("/nav"):
            return nav()
        row = {"rpid": 800, "oid": 42, "mid": 999, "state": 0}
        if request.url.params["root"] == "800":
            return httpx.Response(200, json={"code": 0, "data": {"root": row, "replies": []}})
        rows = [row] if request.url.params["pn"] == "2" else [{"rpid": 1}]
        return httpx.Response(200, json={"code": 0, "data": {"page": {"count": 21}, "replies": rows}})
    bot = adapter(tmp_path, handle)
    assert bot.check_publication(comment_action(), "800").checked_pages == 2
    root = replace(comment_action(), reply_to="", thread_root="")
    assert bot.check_publication(root, "800").status == "account_visible"


@pytest.mark.parametrize("field,value", [("oid", 99), ("mid", 888)])
def test_mismatched_target_is_not_reported_normal(tmp_path, field, value):
    def handle(request):
        if request.url.path.endswith("/nav"):
            return nav()
        row = {"rpid": 800, "oid": 42, "mid": 999, "state": 0, field: value}
        return httpx.Response(200, json={"code": 0, "data": {"replies": [row]}})
    assert adapter(tmp_path, handle).check_publication(comment_action(), "800").status == "unavailable"


def test_qr_login_preserves_server_issued_device_cookies_from_the_session(tmp_path):
    def handle(request):
        if request.url.path.endswith("/nav"):
            return nav()
        return httpx.Response(200, json={"code": 0, "data": {"code": 0}}, headers=[
            ("Set-Cookie", "SESSDATA=fresh; Domain=.bilibili.com; Path=/"),
            ("Set-Cookie", "bili_jct=fresh-csrf; Domain=.bilibili.com; Path=/"),
            ("Set-Cookie", "DedeUserID=999; Domain=.bilibili.com; Path=/"),
            ("Set-Cookie", "ac_time_value=refresh-cookie; Domain=.bilibili.com; Path=/")])
    bot = adapter(tmp_path, handle)
    bot.client.cookies.set("buvid3", "server-issued-device", domain=".bilibili.com")
    bot.client.cookies.set("buvid4", "untrusted", domain="example.invalid")
    assert bot.poll_login("qr-key") == "success"
    saved = json.loads(bot.credentials_path.read_text(encoding="utf-8"))
    assert saved["buvid3"] == "server-issued-device" and "buvid4" not in saved
    assert saved["SESSDATA"] == "fresh" and saved["ac_time_value"] == "refresh-cookie"


def test_qr_login_must_not_reuse_old_auth_when_confirmation_has_no_cookies(tmp_path):
    bot = adapter(tmp_path, lambda request: httpx.Response(200, json={"code": 0, "data": {"code": 0}}))
    before = bot.credentials_path.read_bytes()
    with pytest.raises(PlatformError, match="完整 Cookie"):
        bot.poll_login("qr-key")
    assert bot.credentials_path.read_bytes() == before


def accepted_action(factory, now):
    engine, store, _, platform = factory()
    action = Action("checked", store.agent_id, "bilibili", "robot", platform.ref, "吾辈来聊聊。", now, now + 300,
                    kind="reply", conversation_id="thread")
    store.enqueue_action(action, draft=False, now=now)
    assert engine.dispatch_one(now)
    return engine, store, platform


@pytest.mark.parametrize("restricted", [False, True])
def test_publication_check_persists_without_requeuing_and_retains_ambiguous_checks(factory, now, restricted):
    engine, store, platform = accepted_action(factory, now)
    def probe(action, external_id):
        assert external_id == "98765"
        if restricted:
            raise PlatformError("限制访问", pause=True)
        return PublicationCheck("self_only", "疑似仅自己可见", now + 1, 17, 1)
    result = PublicationService(store, {"bilibili": SimpleNamespace(check_publication=probe)}).check("checked")
    row = store.inspect("actions")[0]
    assert row["state"] == "accepted" and len(platform.sent) == 1
    receipt = json.loads(row["receipt"])
    assert receipt["state"] == "accepted" and receipt["external_id"] == "98765"
    assert receipt["publication_check"]["status"] == ("unavailable" if restricted else "self_only")
    assert not engine.dispatch_one(now + 100)
    assert bool(store.get_setting("platform_paused:bilibili")) == restricted


def test_publication_check_rejects_other_agent_and_stale_receipts(factory, now):
    _, store, _ = accepted_action(factory, now)
    from murasamebot.adapters.persistence.sqlite import SQLiteStore
    other = SQLiteStore(store.path, "another-agent")
    with pytest.raises(ValueError):
        other.publication_target("checked")
    with pytest.raises(ValueError, match="ID 已变化"):
        store.record_publication_check("checked", "different-id", PublicationCheck("self_only", "状态17", now, 17))


def test_private_message_cannot_be_checked_as_publication(tmp_path):
    bot = adapter(tmp_path, lambda request: pytest.fail("private publication check must not reach network"))
    with pytest.raises(ValueError, match="公开评论"):
        bot.check_publication(replace(comment_action(), kind="private_reply", visibility="private"), "800")


def test_dashboard_publication_check_persists_readonly_result(factory, tmp_path, monkeypatch, now):
    from test_architecture_and_config import write_config
    from test_web_disconnects import request
    import murasamebot.bootstrap.web as web
    _, store, platform = accepted_action(factory, now)
    closed = []
    report = PublicationCheck("self_only", "疑似仅自己可见", now + 1, 17, 1)
    probe = SimpleNamespace(check_publication=lambda action, rid: report)
    runtime = SimpleNamespace(platform=probe, modules={"bilibili": SimpleNamespace(adapter=probe)}, close=lambda: closed.append(True))
    monkeypatch.setattr(web, "prepare_store", lambda cfg: store)
    monkeypatch.setattr(web, "build", lambda cfg: runtime)
    handler = request(write_config(tmp_path), method="POST", url="/api/publication/check", body=b'{"id":"checked"}')
    handler.do_POST()
    assert b"200 OK" in handler.wfile.getvalue()
    assert b"self_only" in handler.wfile.getvalue()
    assert closed == [True] and len(platform.sent) == 1
    assert store.inspect("actions")[0]["state"] == "accepted"


def test_reply_is_enqueued_before_optional_likes_and_coins(factory, now):
    engine, store, model, platform = factory(video_like=True, video_coin=True, min_send_gap_seconds=60)
    from test_model_review import inbound
    model.reply["interested"] = True
    original = engine._queue_reactions
    observed = []
    def queue(document, stamp, interested, **kwargs):
        observed.extend(r["kind"] for r in store.inspect("actions"))
        return original(document, stamp, interested, **kwargs)
    engine._queue_reactions = queue
    engine.ingest(inbound(now))
    engine.process_one_event(now)
    assert "reply" in observed
    assert engine.dispatch_one(now)
    assert platform.sent[0][0].kind == "reply"
    assert not engine.dispatch_one(now + 59)
    assert engine.dispatch_one(now + 60)
