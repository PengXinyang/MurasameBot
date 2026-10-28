import json

import httpx
import pytest

from murasamebot.adapters.models.chat_completions import INSTRUCTIONS
from murasamebot.adapters.platforms.bilibili import BilibiliAdapter
from murasamebot.domain.models import Action, CommentContext, ContentRef, Document, Event, PlatformError


REF = ContentRef("bilibili", "video", "42")


def row(key, parent="100", *, author="123", root="100", **extra):
    return {"rpid_str": str(key), "root_str": "0" if str(key) == root else root,
            "parent_str": str(parent), "oid": 42, "type": 1,
            "member": {"mid": author, "uname": "说话者"}, "ctime": 1000,
            "content": {"message": "评论" + str(key)}, **extra}


def adapter(tmp_path, handle):
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps({"DedeUserID": "999"}), encoding="utf-8")
    def transport(request):
        assert request.method == "GET"  # These tests must never publish anything.
        data = handle(request)
        return httpx.Response(200, json=data if "code" in data else {"code": 0, "data": data})
    return BilibiliAdapter(path, read_video_subtitles=False,
                           client=httpx.Client(transport=httpx.MockTransport(transport)))


def test_related_chain_keeps_bot_and_excludes_siblings(tmp_path):
    bot, target = row("200", author="999"), row("300", parent="200")
    api = adapter(tmp_path, lambda _: {"root": row("100", "0"),
                                       "replies": [target, row("400"), bot]})
    comments, coverage = api._comment_chain(REF, "300", "100")
    assert [c["id"] for c in comments] == ["100", "200", "300"]
    assert comments[1]["role"] == "agent" and comments[2]["parent"] == "200"
    assert comments[1]["author_name"] == "说话者" and comments[1]["ctime"] == "1000"
    assert coverage.status == "complete" and coverage.requests == 1


def test_target_on_second_page_and_parent_on_first_page(tmp_path):
    calls = []
    def handle(request):
        page = int(request.url.params["pn"])
        calls.append(page)
        rows = [row("200")] + [row(str(500 + i)) for i in range(19)] if page == 1 else [row("300", "200")]
        return {"root": row("100", "0"), "replies": rows, "page": {"count": 21}}
    comments, coverage = adapter(tmp_path, handle)._comment_chain(REF, "300", "100")
    assert calls == [1, 2] and coverage.status == "complete"
    assert [c["id"] for c in comments] == ["100", "200", "300"]


def test_dialog_lookup_fills_parent_without_more_floor_pages(tmp_path):
    calls = []
    def handle(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/cursor"):
            assert request.url.params["dialog"] == "200"
            return {"replies": [row("200", author="999")]}
        return {"root": row("100", "0"), "replies": [row("300", "200", dialog=200)]}
    comments, coverage = adapter(tmp_path, handle)._comment_chain(REF, "300", "100")
    assert len(calls) == 2 and coverage.requests == 2 and coverage.status == "complete"
    assert comments[1]["role"] == "agent"


def test_missing_deleted_parent_does_not_insert_other_speaker(tmp_path):
    api = adapter(tmp_path, lambda _: {"root": row("100", "0"), "replies": [row("300", "200"), row("400")]})
    comments, coverage = api._comment_chain(REF, "300", "100")
    assert [c["id"] for c in comments] == ["100", "300"]
    assert comments[-1]["parent"] == "200" and coverage.missing_ids == ("200",)
    assert coverage.status == "partial"


@pytest.mark.parametrize("extra", [{"root_str": "777"}, {"oid": 777}, {"type": 11}])
def test_foreign_thread_or_content_is_never_used(tmp_path, extra):
    api = adapter(tmp_path, lambda _: {"root": row("100", "0"), "replies": [row("300", **extra)]})
    comments, coverage = api._comment_chain(REF, "300", "100")
    assert [c["id"] for c in comments] == ["100"] and "300" in coverage.missing_ids


def test_missing_parent_field_is_not_inferred_from_root(tmp_path):
    target = row("300")
    del target["parent_str"]
    comments, coverage = adapter(tmp_path, lambda _: {"root": row("100", "0"), "replies": [target]})._comment_chain(REF, "300", "100")
    assert comments[-1]["parent"] == "" and "missing_parent_link" in coverage.issues


def test_request_budget_is_three_pages_plus_one_dialog(tmp_path):
    calls = []
    def handle(request):
        calls.append(request.url.path)
        if request.url.path.endswith("cursor"):
            return {"replies": []}
        page = int(request.url.params["pn"])
        rows = [row(str(500 + page * 20 + i)) for i in range(19)] + [row("300", "200", dialog=200)]
        return {"root": row("100", "0"), "replies": rows, "page": {"count": 10000}}
    _, coverage = adapter(tmp_path, handle)._comment_chain(REF, "300", "100")
    assert len(calls) == coverage.requests == 4
    assert coverage.status == "partial" and "page_limit" in coverage.issues


@pytest.mark.parametrize("loop", [True, False])
def test_cycle_and_depth_are_bounded(tmp_path, loop):
    rows = [row(str(i), str(i + 1)) for i in range(200, 219)]
    if loop:
        rows[1]["parent_str"] = "200"
    comments, coverage = adapter(tmp_path, lambda _: {"root": row("100", "0"), "replies": rows})._comment_chain(REF, "200", "100")
    assert len(comments) <= 12 and len({c["id"] for c in comments}) == len(comments)
    assert ("cycle" if loop else "depth_limit") in coverage.issues


def test_text_budget_and_metadata_roundtrip(tmp_path):
    rows = [row(str(i), str(i - 1) if i > 200 else "100", content={"message": "文" * 2000}) for i in range(200, 210)]
    comments, coverage = adapter(tmp_path, lambda _: {"root": row("100", "0"), "replies": rows})._comment_chain(REF, "209", "100")
    assert sum(len(c["text"]) for c in comments) <= 6000
    assert comments[-1]["id"] == "209" and "text_limit" in coverage.issues
    doc = Document(REF, "标题", "简介", "metadata", comments=comments, comment_context=coverage)
    assert Document.from_dict(json.loads(json.dumps(doc.public_context()))) == doc
    old = doc.public_context()
    del old["comment_context"]
    assert Document.from_dict(old).comment_context is None


def test_missing_context_is_partial_but_rate_limit_still_pauses(tmp_path):
    api = adapter(tmp_path, lambda _: {"code": -404})
    comments, coverage = api._comment_chain(REF, "300", "100")
    assert not comments and coverage.status == "partial" and coverage.requests == 1
    assert "read_unavailable" in coverage.issues
    api = adapter(tmp_path, lambda _: {"code": -412})
    with pytest.raises(PlatformError) as exc:
        api._comment_chain(REF, "300", "100")
    assert exc.value.pause


def test_read_document_passes_chain_and_browsing_still_excludes_self(tmp_path):
    def handle(request):
        if request.url.path.endswith("view"):
            return {"bvid": "BV123", "title": "标题", "desc": "简介"}
        return {"root": row("100", "0", author="999"), "replies": [row("300")]}
    doc = adapter(tmp_path, handle).read(REF, reply_to="300", root="100")
    assert doc.comment_context.status == "complete" and doc.comments[0]["role"] == "agent"
    api = adapter(tmp_path, lambda _: {"replies": [row("100", "0", author="999"), row("200")]})
    assert [c["id"] for c in api._comments(REF, "", "")] == ["200"]


def test_reply_model_gets_speaker_target_and_keeps_outgoing_address(factory, now, tmp_path):
    engine, store, model, platform = factory()
    api = adapter(tmp_path, lambda _: {"root": row("100", "0"), "replies": [row("300", "200"), row("200", author="999")]})
    comments, coverage = api._comment_chain(REF, "300", "100")
    platform.documents[platform.ref.key] = Document(platform.ref, "标题", "简介", "metadata", comments=comments, comment_context=coverage)
    event = Event("chain", "bilibili", "robot", "123", "video:42", "mention", "当前问题", now,
                  content=platform.ref, reply_to="300", thread_root="100", actor_name="提问者")
    engine.ingest(event)
    engine.process_one_event(now)
    payload = next(p for op, p in model.calls if op == "reply")
    assert payload["reply_target"] == {"id": "300", "root": "100", "author_id": "123", "author_name": "提问者"}
    assert payload["content"]["comment_context"]["status"] == "complete"
    assert payload["content"]["comments"][1]["role"] == "agent"
    action = json.loads(store.inspect("actions")[0]["payload"])
    assert action["reply_to"] == "300" and action["thread_root"] == "100"
    assert "parent" in INSTRUCTIONS["reply"] and "missing_ids" in INSTRUCTIONS["reply"]


def test_independent_review_refreshes_other_speakers_cached_chain(factory, now):
    engine, store, model, platform = factory()
    wrong = Document(platform.ref, "标题", "简介", "metadata",
                     comments=({"id": "888", "text": "其他人的问题"},),
                     comment_context=CommentContext("888", "100", "complete"))
    right = Document(platform.ref, "标题", "简介", "metadata",
                     comments=({"id": "300", "text": "本次问题"},),
                     comment_context=CommentContext("300", "100", "complete"))
    store.cache_document(wrong, now)
    calls = []
    def read(ref, *, reply_to="", root="", context=None):
        calls.append((reply_to, root))
        return right
    platform.read = read
    action = Action("review-chain", store.agent_id, "bilibili", "robot", platform.ref,
                    "回复", now, now + 100, reply_to="300", thread_root="100")
    engine._review_action(action, now)
    payload = next(p for op, p in model.calls if op == "review_action")
    assert calls == [("300", "100")]
    assert payload["target_comment"]["id"] == "300"
    assert payload["content"]["comment_context"]["target_id"] == "300"
