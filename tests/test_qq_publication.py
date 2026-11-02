"""QQ private text bypasses publication review, never authorization or outbox gates."""
from dataclasses import replace
import json

import pytest

from murasamebot.adapters.models.chat_completions import ChatCompletionsModel, PUBLICATION_RULES
from murasamebot.application.review import publication_review_required
from murasamebot.bootstrap.platforms.qq import PRIVATE_POLICY, QQModule
from murasamebot.domain.models import Action
from test_qq import setup, raw


@pytest.mark.parametrize("strategy", ["inline", "independent"])
@pytest.mark.parametrize("draft", [False, True])
@pytest.mark.parametrize("verdict", [{}, {"can_send": False, "review_reason": "旧格式判断"}])
def test_private_reply_reaches_sender_without_a_publication_verdict(factory, now, strategy, draft, verdict):
    e, s, adapter, writes = setup(factory)
    e.private_policies["qq"] = dict(PRIVATE_POLICY)
    e.private_access["qq"] = adapter
    e.sinks["qq"] = adapter
    e.settings = replace(e.settings, model_review=True, review_strategy=strategy, draft=draft)
    calls = []
    def decide(op, payload):
        calls.append(op)
        if op == "private_chat":
            return {"decision": "reply", "text": "吾辈在这里。", **verdict}
        assert op == "extract_intent"
        return {"kind": "none"}
    e.model.decide = decide
    try:
        event = QQModule("qq", adapter).normalize_event(adapter.parse(raw(), now))
        e.ingest(event)
        assert e.process_one_event(now)
        row = s.inspect("actions")[0]
        assert row["state"] == "queued"
        assert json.loads(row["payload"])["review_state"] == "not_required"
        assert s.queue_status(review_exempt_platforms=("qq",))["review_ready_actions"] == 0
        assert not e.review_one(now)
        # Rebuilding in global draft mode must not turn new QQ text into a draft.
        s.enable_review_mode(review_exempt_platforms=("qq",))
        assert s.inspect("actions")[0]["state"] == "queued"
        assert e.dispatch_one(now, pre_reviewed_only=True)
        assert len(writes) == 1 and "review_action" not in calls
        assert s.inspect("actions")[0]["state"] == "accepted"
    finally:
        adapter.close()


@pytest.mark.parametrize("change", ["forget", "unfriend", "pause"])
def test_no_review_private_messages_still_recheck_before_write(factory, now, change):
    e, s, adapter, writes = setup(factory)
    e.private_policies["qq"] = dict(PRIVATE_POLICY)
    e.private_access["qq"] = adapter
    e.sinks["qq"] = adapter
    e.model.decide = lambda op, p: {"decision": "reply", "text": "吾辈在这里。"} if op == "private_chat" else {"kind": "none"}
    try:
        event = QQModule("qq", adapter).normalize_event(adapter.parse(raw(), now))
        e.ingest(event)
        assert e.process_one_event(now)
        if change == "forget":
            s.forget_source(event.event_id)
        elif change == "unfriend":
            adapter.allows_private_reply = lambda *args: False
        else:
            s.set_setting("paused", True)
        e.dispatch_one(now, pre_reviewed_only=True)
        assert not writes
    finally:
        adapter.close()


@pytest.mark.parametrize("platform,visibility,kind,required", [
    ("qq", "private", "private_reply", False),
    ("qq", "private", "task_result", False),
    ("qq", "private", "proactive_private", False),
    ("qq", "private", "call_reply", True),
    ("qq", "group", "group_reply", True),
    ("qq", "public", "autonomous_post", True),
    ("bilibili", "private", "private_reply", True),
    ("bilibili", "public", "recommendation", True),
])
def test_exemption_stays_with_the_private_text_channel(platform, visibility, kind, required):
    action = Action("id", "agent", platform, "bot", None, "文字", 0, 100,
                    visibility=visibility, kind=kind)
    assert publication_review_required(action, ("qq",)) is required


@pytest.mark.parametrize("operation", ["private_chat", "private_task_result", "autonomous_private"])
def test_model_instructions_follow_the_program_owned_private_policy(operation):
    normal = ChatCompletionsModel.operation_instructions(operation, {})
    exempt = ChatCompletionsModel.operation_instructions(operation, {"private_policy": PRIVATE_POLICY})
    assert PUBLICATION_RULES in normal
    assert PUBLICATION_RULES not in exempt
    assert "不要求can_send或review_reason" in exempt


def test_legacy_private_draft_is_not_automatically_replayed(factory, now):
    e, s, adapter, writes = setup(factory)
    e.private_policies["qq"] = dict(PRIVATE_POLICY)
    e.sinks["qq"] = adapter
    try:
        # An old draft remains a deliberate manual action; no migration releases it.
        with s.connection() as db:
            db.execute("INSERT INTO actions(agent_id,action_id,payload,content_key,dedup_key,kind,state,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?)",
                       (s.agent_id, "old", json.dumps(Action("old",s.agent_id,"qq","456",None,"旧草稿",now,now+100,
                        visibility="private",kind="private_reply").to_dict()), "", "", "private_reply", "draft", now, now+100))
        assert not e.dispatch_one(now, pre_reviewed_only=True)
        assert s.inspect("actions")[0]["state"] == "draft" and not writes
    finally:
        adapter.close()
