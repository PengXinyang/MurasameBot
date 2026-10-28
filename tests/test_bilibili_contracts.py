from dataclasses import replace
import json
from urllib.parse import parse_qs

import httpx
import pytest

from murasamebot.adapters.platforms.bilibili import BilibiliAdapter
from murasamebot.domain.models import Action, ContentRef, PlatformError


def credentials(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps({"SESSDATA": "test-secret", "bili_jct": "csrf", "DedeUserID": "999"}), encoding="utf-8")
    return path


def nav_data():
    return {"isLogin": True, "mid": 999, "wbi_img": {
        "img_url": "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png",
        "sub_url": "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png"}}


def notification(kind, rpid="700"):
    return {"id": 100, "reply_time": 1000, "user": {"mid": 123, "nickname": "某人"},
            "item": {"source_id": int(rpid), "subject_id": 42, "business_id": kind,
                     "source_content": "请问这里怎么理解？", "root_id": 600}}


def test_mention_and_reply_notifications_share_same_dedup_id():
    reply = BilibiliAdapter.notification_event(notification(1), "reply", "999", 1000)
    mention = BilibiliAdapter.notification_event(notification(1), "mention", "999", 1000)
    assert reply.event_id == mention.event_id
    assert reply.reply_to == "700" and reply.thread_root == "600"
    assert reply.content == ContentRef("bilibili", "video", "42")


def test_dynamic_notification_uses_actual_business_comment_type():
    event = BilibiliAdapter.notification_event(notification(11), "mention", "999", 1000)
    assert event.content.kind == "draw"
    assert BilibiliAdapter.notification_event(notification(999), "mention", "999", 1000) is None


def test_post_body_mention_is_not_mistaken_for_comment_mention(tmp_path):
    item = notification(11)
    item["item"].update(type="dynamic", uri="https://www.bilibili.com/opus/900", source_id=900)
    event = BilibiliAdapter.notification_event(item, "mention", "999", 1000)
    assert event.kind == "post_mention" and not event.reply_to
    def handle(request):
        return httpx.Response(200, json={"code": 0, "data": {"item": {"id_str": "900",
            "basic": {"comment_type": 11, "comment_id_str": "42"}, "modules": {"module_author": {"mid": 123}}}}})
    adapter = BilibiliAdapter(credentials(tmp_path), client=httpx.Client(transport=httpx.MockTransport(handle)))
    resolved = adapter._resolve_post_mention(event)
    assert resolved.content == ContentRef("bilibili", "draw", "42") and resolved.share_content.external_id == "900"
    assert adapter._resolve_post_mention(replace(event, actor_id="different")) is None
    item["item"]["source_id"] = 700
    comment = BilibiliAdapter.notification_event(item, "mention", "999", 1000)
    assert comment.kind == "mention" and comment.reply_to == "700"


def test_image_dynamic_does_not_guess_comment_oid_from_dynamic_id():
    item = {"id_str": "900", "type": "DYNAMIC_TYPE_DRAW", "basic": {"comment_id_str": "200", "comment_type": 11}}
    assert BilibiliAdapter.dynamic_reference(item) == ContentRef("bilibili", "draw", "200")
    assert BilibiliAdapter.dynamic_reference({"id_str": "900", "type": "DYNAMIC_TYPE_DRAW"}) is None


def test_draw_notification_preserves_dynamic_id_separate_from_comment_id():
    source = notification(11)
    source["item"]["uri"] = "https://www.bilibili.com/opus/900"
    event = BilibiliAdapter.notification_event(source, "mention", "999", 1000)
    assert event.content == ContentRef("bilibili", "draw", "42")
    assert event.share_content == ContentRef("bilibili", "post", "900")


def test_draw_read_uses_modern_dynamic_endpoint_and_checks_comment_binding(tmp_path):
    comment_oid = ["42"]
    paths = []
    def handle(request):
        paths.append(request.url.path)
        if request.url.path.endswith("/detail"):
            assert request.url.params["id"] == "900"
            return httpx.Response(200, json={"code": 0, "data": {"item": {
                "basic": {"comment_type": 11, "comment_id_str": comment_oid[0]},
                "modules": {"module_author": {"mid": 123}, "module_dynamic": {"desc": {"text": "正文"}}}}}})
        assert request.url.path == "/x/v2/reply/main" and request.url.params["oid"] == "42"
        return httpx.Response(200, json={"code": 0, "data": {"replies": []}})
    adapter = BilibiliAdapter(credentials(tmp_path), client=httpx.Client(transport=httpx.MockTransport(handle)))
    doc = adapter.read(ContentRef("bilibili", "draw", "42"), context=ContentRef("bilibili", "post", "900"))
    assert doc.url == "https://www.bilibili.com/opus/900" and doc.text == "正文"
    assert not any("doc_detail" in path for path in paths)
    comment_oid[0] = "different-comment-section"
    with pytest.raises(PlatformError, match="不匹配"):
        adapter.read(ContentRef("bilibili", "draw", "42"), context=ContentRef("bilibili", "post", "900"))


def test_real_mention_mapping_and_thread_parent_are_encoded(tmp_path):
    writes = []
    def handle(request):
        if request.url.path.endswith("/nav"):
            return httpx.Response(200, json={"code": 0, "data": nav_data()})
        if request.url.path.endswith("/card"):
            return httpx.Response(200, json={"code": 0, "data": {"card": {"name": "当前主人昵称"}}})
        writes.append(parse_qs(request.content.decode()))
        return httpx.Response(200, json={"code": 0, "data": {"reply": {"rpid": 12345, "content": {"members": [{"mid": 424242}]}}}})
    adapter = BilibiliAdapter(credentials(tmp_path), client=httpx.Client(transport=httpx.MockTransport(handle)))
    action = Action("a", "agent", "bilibili", "999", ContentRef("bilibili", "video", "42"), "吾辈觉得这期值得看看。", 1000, 2000,
                    reply_to="700", thread_root="600", mentions=("owner",))
    result = adapter.send(action, {"owner": "424242"})
    assert result.state == "accepted" and result.mention_resolved
    assert writes[0]["parent"] == ["700"] and writes[0]["root"] == ["600"]
    assert writes[0]["message"][0].startswith("@当前主人昵称 ")
    assert json.loads(writes[0]["at_name_to_mid"][0]) == {"当前主人昵称": 424242}


def test_post_timeout_is_unknown_and_not_retried(tmp_path):
    posts = []
    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json={"code": 0, "data": nav_data()})
        posts.append(request)
        raise httpx.ReadTimeout("timeout")
    adapter = BilibiliAdapter(credentials(tmp_path), client=httpx.Client(transport=httpx.MockTransport(handle)))
    action = Action("a", "agent", "bilibili", "999", ContentRef("bilibili", "video", "42"), "吾辈想聊聊这个。", 1000, 2000)
    assert adapter.send(action, {}).state == "unknown"
    assert len(posts) == 1


def test_wrong_robot_identity_blocks_post(tmp_path):
    def handle(request):
        assert request.method == "GET"
        return httpx.Response(200, json={"code": 0, "data": {"isLogin": True, "mid": 888}})
    adapter = BilibiliAdapter(credentials(tmp_path), client=httpx.Client(transport=httpx.MockTransport(handle)))
    action = Action("a", "agent", "bilibili", "999", ContentRef("bilibili", "video", "42"), "吾辈想聊聊这个。", 1000, 2000)
    with pytest.raises(PlatformError) as exc:
        adapter.send(action, {})
    assert exc.value.pause


def test_captcha_error_pauses_instead_of_retrying(tmp_path):
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"code": -352})
    adapter = BilibiliAdapter(credentials(tmp_path), client=httpx.Client(transport=httpx.MockTransport(handle)))
    with pytest.raises(PlatformError) as exc:
        adapter.account()
    assert exc.value.pause and len(requests) == 1


def test_transport_does_not_send_cookies_to_untrusted_urls(tmp_path):
    adapter = BilibiliAdapter(credentials(tmp_path), client=httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail("network must not be reached"))))
    with pytest.raises(PlatformError):
        adapter._request("https://attacker.invalid/collect")


def test_final_gate_can_stop_write_after_account_lookup(tmp_path):
    calls = []
    def handle(request):
        calls.append(request.method)
        assert request.method == "GET", "最终校验拒绝后不能 POST"
        return httpx.Response(200, json={"code": 0, "data": nav_data()})
    adapter = BilibiliAdapter(credentials(tmp_path), client=httpx.Client(transport=httpx.MockTransport(handle)))
    action = Action("a", "agent", "bilibili", "999", ContentRef("bilibili", "video", "42"), "吾辈想聊聊这个。", 1000, 2000)
    result = adapter.send(action, {}, before_write=lambda: False)
    assert result.state == "failed" and calls == ["GET"]
