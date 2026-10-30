from dataclasses import replace
import json
from urllib.parse import parse_qs

import httpx
import pytest

from murasamebot.adapters.platforms.bilibili import BilibiliAdapter
from murasamebot.domain.models import Action, ContentRef, Document, Event, PlatformError
from test_bilibili_contracts import credentials


def reaction(kind="coin", ref=None, **kw):
    return Action("reaction", "agent", "bilibili", "999", ref or ContentRef("bilibili", "video", "42"),
                  "为感兴趣的内容互动", 1000, 2000, kind=kind, amount=1 if kind == "coin" else 0, **kw)


def adapter_for(tmp_path, *, balance=5, coins=0, liked=False, timeout=False):
    posts = []
    def handle(request):
        path = request.url.path
        if path.endswith("/nav"):
            data = {"isLogin": True, "mid": 999, "money": balance}
        elif path.endswith("/coins"):
            data = {"multiply": coins}
        elif path.endswith("/relation"):
            data = {"like": liked}
        elif path.endswith("/finger/spi"):
            data = {"b_3": "server-issued-device", "b_4": "server-issued-device-four"}
        elif path.endswith("/detail"):
            data = {"item": {"id_str": "900", "type": "DYNAMIC_TYPE_DRAW",
                            "basic": {"comment_id_str": "42", "comment_type": 11},
                            "modules": {"module_stat": {"like": {"status": liked}}}}}
        else:
            assert request.method == "POST"
            posts.append(request)
            if timeout:
                raise httpx.ReadTimeout("ambiguous")
            data = {}
        return httpx.Response(200, json={"code": 0, "data": data})
    return BilibiliAdapter(credentials(tmp_path), client=httpx.Client(transport=httpx.MockTransport(handle))), posts


@pytest.mark.parametrize("balance", [0, 0.5])
def test_no_coin_request_without_one_whole_coin(tmp_path, balance):
    adapter, posts = adapter_for(tmp_path, balance=balance)
    result = adapter.send(reaction(), {})
    assert result.state == "failed" and "不足" in result.detail and not posts


def test_coin_always_exactly_one_and_no_implicit_like(tmp_path):
    adapter, posts = adapter_for(tmp_path)
    assert adapter.send(reaction(), {}).state == "accepted"
    assert len(posts) == 1 and posts[0].url.path == "/x/web-interface/coin/add"
    fields = parse_qs(posts[0].content.decode())
    assert fields["multiply"] == ["1"] and fields["select_like"] == ["0"]
    assert posts[0].headers["origin"] == "https://www.bilibili.com"
    assert posts[0].headers["referer"] == "https://www.bilibili.com/video/av42/"
    assert "buvid3=server-issued-device" in posts[0].headers["cookie"]


def test_coin_matches_web_page_context_using_local_session(tmp_path):
    adapter, posts = adapter_for(tmp_path)
    assert adapter.send(reaction(), {}).state == "accepted"
    request, = posts
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    # Keep the blank field: absence is not the successful web form's contract.
    assert parse_qs(request.content.decode(), keep_blank_values=True) == {
        "aid": ["42"], "multiply": ["1"], "select_like": ["0"],
        "cross_domain": ["true"], "from_spmid": [""], "spmid": ["333.788.0.0"],
        "statistics": ['{"appId":100,"platform":5}'], "eab_x": ["2"],
        "ramval": ["0"], "source": ["web_normal"], "ga": ["1"], "csrf": ["csrf"],
    }
    query = dict(request.url.params)
    assert set(query) == {"x-bili-locale-json", "x-bili-device-req-json"}
    assert json.loads(query["x-bili-locale-json"]) == {
        "c_locale": {"language": "zh", "script": "Hans"}, "always_translate": False,
    }
    assert json.loads(query["x-bili-device-req-json"]) == {
        "platform": "web", "device": "pc", "spmid": "333.788", "mobi_app": "web_cn",
    }
    # Authentication still comes from this test session, never a saved capture.
    assert "SESSDATA=test-secret" in request.headers["cookie"]
    assert "bili_jct=csrf" in request.headers["cookie"]


@pytest.mark.parametrize("dynamic", [False, True])
def test_coin_page_context_does_not_change_later_like_requests(tmp_path, dynamic):
    adapter, posts = adapter_for(tmp_path)
    assert adapter.send(reaction(), {}).state == "accepted"
    action = reaction("like", ContentRef("bilibili", "draw", "42"),
                      context=ContentRef("bilibili", "post", "900")) if dynamic else reaction("like")
    assert adapter.send(action, {}).state == "accepted"
    assert len(posts) == 2
    request = posts[1]
    assert dict(request.url.params) == {"csrf": "csrf"}
    if dynamic:
        assert request.url.path == "/x/dynamic/feed/dyn/thumb"
        assert json.loads(request.content) == {"dyn_id_str": "900", "up": 1}
    else:
        assert request.url.path == "/x/web-interface/archive/like"
        assert parse_qs(request.content.decode()) == {"aid": ["42"], "csrf": ["csrf"], "like": ["1"]}


@pytest.mark.parametrize("coins", [1, 2])
def test_previously_coined_video_is_not_coined_again(tmp_path, coins):
    adapter, posts = adapter_for(tmp_path, coins=coins)
    assert adapter.send(reaction(), {}).state == "accepted" and not posts


@pytest.mark.parametrize("amount", [0, 2, True, "1"])
def test_invalid_coin_amount_cannot_bypass_gate(tmp_path, amount):
    adapter, posts = adapter_for(tmp_path)
    with pytest.raises(PlatformError, match="1 枚"):
        adapter.send(replace(reaction(), amount=amount), {})
    assert not posts


@pytest.mark.parametrize("balance", [None, "5", -1, float("nan")])
def test_unknown_or_invalid_balance_does_not_spend(tmp_path, balance):
    adapter, posts = adapter_for(tmp_path, balance=balance)
    # JSON cannot encode NaN; simulate it at account boundary instead.
    if isinstance(balance, float):
        adapter.account = lambda: {"uid": "999", "money": balance}
    with pytest.raises(PlatformError):
        adapter.send(reaction(), {})
    assert not posts


@pytest.mark.parametrize("kind", ["coin", "like"])
def test_final_gate_blocks_reactions(tmp_path, kind):
    adapter, posts = adapter_for(tmp_path)
    assert adapter.send(reaction(kind), {}, before_write=lambda: False).state == "failed"
    assert not posts


def test_like_is_explicit_set_not_toggle(tmp_path):
    adapter, posts = adapter_for(tmp_path)
    assert adapter.send(reaction("like"), {}).state == "accepted"
    assert parse_qs(posts[0].content.decode())["like"] == ["1"]


def test_draw_like_uses_verified_dynamic_id(tmp_path):
    adapter, posts = adapter_for(tmp_path)
    action = reaction("like", ContentRef("bilibili", "draw", "42"), context=ContentRef("bilibili", "post", "900"))
    assert adapter.send(action, {}).state == "accepted"
    assert posts[0].url.path == "/x/dynamic/feed/dyn/thumb"
    assert json.loads(posts[0].content) == {"dyn_id_str": "900", "up": 1}
    with pytest.raises(PlatformError, match="评论区"):
        adapter.send(replace(action, content=ContentRef("bilibili", "draw", "43")), {})
    with pytest.raises(PlatformError, match="实际动态"):
        adapter.send(replace(action, context=None), {})
    assert len(posts) == 1


def test_reaction_timeout_is_unknown_and_only_one_request(tmp_path):
    adapter, posts = adapter_for(tmp_path, timeout=True)
    assert adapter.send(reaction(), {}).state == "unknown" and len(posts) == 1


def test_silent_interest_queues_independent_reactions_and_deduplicates(factory, now):
    engine, store, model, platform = factory(video_like=True, video_coin=True, model_review=True)
    model.evaluation = {"decision": "skip", "interested": True, "reason": "主题有趣"}
    doc = platform.read(platform.ref)
    assert engine._evaluate(doc, now) is False
    engine._evaluate(doc, now + 1)
    assert {row["kind"] for row in store.inspect("actions")} == {"like", "coin"}
    assert len(store.inspect("actions")) == 2
    assert not store.has_action_for_content(doc.ref.key, now)
    assert engine.dispatch_one(now)
    assert all(op != "review_action" for op, _ in model.calls)
    assert engine.dispatch_one(now + 2)
    assert engine.dispatch_one(now + 4) is False


def test_reactions_do_not_block_recommendation(factory, now):
    engine, store, model, platform = factory(video_like=True, video_coin=True)
    model.evaluation["interested"] = True
    assert engine._evaluate(platform.read(platform.ref), now)
    assert {r["kind"] for r in store.inspect("actions")} == {"recommendation", "like", "coin"}


def test_dynamic_can_like_without_reply_and_never_coin(factory, now):
    engine, store, model, platform = factory(dynamic_like=True, video_coin=True)
    ref = ContentRef("bilibili", "draw", "42")
    platform.documents[ref.key] = Document(ref, "动态", "吾辈感兴趣的内容", "text", parent=ContentRef("bilibili", "post", "900"))
    model.reply = {"decision": "skip", "interested": True}
    engine.ingest(Event("event", "bilibili", "robot", "123", "public-thread", "mention", "聊聊", now, content=ref))
    assert engine.process_one_event(now)
    actions = store.inspect("actions")
    assert len(actions) == 1 and actions[0]["kind"] == "like"
    action = Action.from_dict(json.loads(actions[0]["payload"]))
    assert action.context.external_id == "900" and not action.proactive


def test_browsing_reactions_wait_for_browsing_window(factory, now):
    engine, store, model, platform = factory(video_coin=True, browsing_enabled=False)
    engine._queue_reactions(platform.read(platform.ref), now, True,memory_epoch=store.get_setting("memory_epoch",0))
    assert len(store.inspect("actions"))==1
    assert not engine.dispatch_one(now)


def test_metadata_video_can_be_recommended_without_subtitles(factory, now):
    engine, store, model, platform = factory(allow_metadata_video=True)
    platform.documents[platform.ref.key] = Document(platform.ref, "摄影入门", "曝光和构图介绍", "metadata", "123")
    assert engine.browse(now) == 1
    assert store.inspect("actions")[0]["kind"] == "recommendation"
