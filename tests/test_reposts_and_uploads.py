from dataclasses import replace
from io import BytesIO
import json
from types import SimpleNamespace

import httpx
from PIL import Image

from murasamebot.bootstrap.runtime import Runtime
from murasamebot.adapters.platforms.bilibili import BilibiliAdapter
from murasamebot.domain.models import Action, ContentRef, Document, Event
from test_artwork import WORK
from test_bilibili_contracts import credentials


def test_comment_and_repost_independently_queue_and_binding_revocation_cancels_share(factory, now):
    engine, store, model, platform = factory()
    ref = ContentRef("bilibili", "dynamic", "700")
    platform.documents[ref.key] = Document(ref, "主人新动态", "今天分享练习心得", "text", "424242")
    original = model.decide
    model.decide = lambda op, payload: {"decision": "share", "topic_class":"casual", "text": "主人更新啦，吾辈也来分享这份小欢喜。"} if op == "owner_repost" else original(op, payload)
    comment = Event("owner-comment", "bilibili", "robot", "424242", "owner-post", "owner_content", "新动态", now, content=ref)
    repost = replace(comment, event_id="owner-repost", kind="owner_repost")
    engine.ingest(comment)
    engine.ingest(repost)
    engine.process_one_event(now)
    engine.process_one_event(now)
    assert {r["kind"] for r in store.inspect("actions")} == {"owner_comment", "owner_repost"}
    store.revoke_binding("owner", "bilibili")
    assert next(r for r in store.inspect("actions") if r["kind"] == "owner_repost")["state"] == "cancelled"


def test_owner_poll_keeps_video_comment_oid_and_repost_dynamic_id_separate(factory, now):
    engine, store, model, platform = factory()
    platform.account_id = "robot"
    platform.notifications = lambda stamp: []
    events = []
    platform.owner_content = lambda uid, stamp: list(events)
    config = SimpleNamespace(data={"owner_person_id": "owner", "bilibili": {"owner_new_videos": False, "owner_new_dynamics": True,
                                                                             "repost_owner_videos": True, "repost_owner_dynamics": True}})
    runtime = Runtime(config, store, platform, model, engine)
    runtime.poll(now)
    events.append(Event("video", "bilibili", "robot", "424242", "post", "owner_content", "新视频", now + 2,
                        content=ContentRef("bilibili", "video", "42"), share_content=ContentRef("bilibili", "post", "900")))
    runtime.poll(now + 3)
    rows = {r["event_id"]: r for r in store.inspect("events")}
    assert rows["video"]["state"] == "baseline"  # Comments independently disabled.
    share = rows["video:repost"]
    assert share["state"] == "pending"
    payload = json.loads(share["payload"])
    assert payload["content"]["external_id"] == "900" and payload["share_content"]["external_id"] == "42"


def test_native_repost_checks_author_and_uses_dynamic_reference(tmp_path):
    writes = []
    def handle(request):
        if request.url.path.endswith("/nav"):
            return httpx.Response(200, json={"code": 0, "data": {"isLogin": True, "mid": 999}})
        if request.method == "GET":
            return httpx.Response(200, json={"code": 0, "data": {"item": {"modules": {"module_author": {"mid": 424242}}}}})
        writes.append(json.loads(request.content))
        assert request.url.path == "/x/dynamic/feed/create/dyn"
        return httpx.Response(200, json={"code": 0, "data": {"dyn_id_str": "888"}})
    adapter = BilibiliAdapter(credentials(tmp_path), client=httpx.Client(transport=httpx.MockTransport(handle)))
    action = Action("repost", "agent", "bilibili", "999", ContentRef("bilibili", "post", "900"), "主人更新啦。", 1000, 2000,
                    kind="owner_repost", source_author_id="424242")
    assert adapter.send(action, {}, before_write=lambda: True).external_id == "888"
    assert writes[0]["web_repost_src"] == {"dyn_id_str": "900"}
    assert writes[0]["dyn_req"]["scene"] == 4


def test_image_upload_is_cookie_isolated_credited_and_final_gate_runs_after_upload(tmp_path):
    image = BytesIO()
    Image.new("RGB", (10, 10), "green").save(image, format="PNG")
    posts = []
    def download(request):
        assert request.url.host == "i.pximg.net" and "cookie" not in request.headers
        return httpx.Response(200, content=image.getvalue())
    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json={"code": 0, "data": {"isLogin": True, "mid": 999}})
        posts.append(request)
        if request.url.path.endswith("upload_bfs"):
            assert b"file_up" in request.content
            return httpx.Response(200, json={"code": 0, "data": {"image_url": "https://i0.hdslb.com/bfs/test.png", "image_width": 10, "image_height": 10}})
        body = json.loads(request.content)
        assert body["dyn_req"]["scene"] == 2
        text = body["dyn_req"]["content"]["contents"][0]["raw_text"]
        assert WORK.artist_name in text and WORK.source_url in text
        return httpx.Response(200, json={"code": 0, "data": {"dyn_id_str": "888"}})
    adapter = BilibiliAdapter(credentials(tmp_path), client=httpx.Client(transport=httpx.MockTransport(handle)),
                              image_client=httpx.Client(transport=httpx.MockTransport(download)))
    action = Action("image", "agent", "bilibili", "999", WORK.ref, "吾辈想分享这份小欢喜。", 1000, 2000,
                    kind="artwork_share", artwork={**WORK.to_dict(), "delivery": "image", "permission_id": "test-grant"})
    assert adapter.send(action, {}, before_write=lambda: True).state == "accepted"
    assert len(posts) == 2
    posts.clear()
    assert adapter.send(action, {}, before_write=lambda: len(posts) == 0).state == "failed"
    assert len(posts) == 1 and posts[0].url.path.endswith("upload_bfs")  # Revoked after upload, no public dynamic.


def test_ambiguous_dynamic_result_is_never_retried(tmp_path):
    posts = []
    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json={"code": 0, "data": {"isLogin": True, "mid": 999}})
        posts.append(request)
        raise httpx.ReadTimeout("unknown")
    adapter = BilibiliAdapter(credentials(tmp_path), client=httpx.Client(transport=httpx.MockTransport(handle)))
    action = Action("link", "agent", "bilibili", "999", WORK.ref, "吾辈发现了角色画作。", 1000, 2000,
                    kind="artwork_share", artwork={**WORK.to_dict(), "delivery": "link"})
    assert adapter.send(action, {}).state == "unknown" and len(posts) == 1
