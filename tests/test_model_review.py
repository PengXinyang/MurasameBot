from dataclasses import replace
import json

import pytest

from murasamebot.domain.models import ContentRef, Event, ModelError, Receipt
from test_private_messages import chat_engine, private_event


def inbound(now):
    return Event("mention-review", "bilibili", "robot", "123", "public-thread", "mention", "这里该怎么理解？", now,
                 content=ContentRef("bilibili", "video", "10001"), reply_to="700", thread_root="600")


def review_model(model, decision="approve", callback=None):
    original = model.decide
    def decide(op, payload):
        if op == "review_action":
            model.calls.append((op, payload))
            if callback:
                callback()
            if decision == "error":
                raise ModelError("service unavailable")
            return {"decision": decision, "reason": "符合当前对话" if decision == "approve" else "需要人工复核"}
        return original(op, payload)
    model.decide = decide


def test_model_pass_automatically_sends_and_records_review(factory, now):
    engine, store, model, platform = factory(model_review=True)
    review_model(model)
    engine.ingest(inbound(now))
    engine.process_one_event(now)
    assert engine.dispatch_one(now)
    assert len(platform.sent) == 1 and store.inspect("actions")[0]["state"] == "accepted"
    assert json.loads(store.inspect("actions")[0]["payload"])["review_state"] == "passed"


@pytest.mark.parametrize("decision,state", [("reject", "rejected"), ("error", "unavailable"), ("invalid", "unavailable")])
def test_model_failure_goes_to_human_draft_and_manual_approval_sends(factory, now, decision, state):
    engine, store, model, platform = factory(model_review=True)
    review_model(model, decision)
    engine.ingest(inbound(now))
    engine.process_one_event(now)
    engine.dispatch_one(now)
    row = store.inspect("actions")[0]
    assert row["state"] == "draft" and json.loads(row["payload"])["review_state"] == state
    assert not platform.sent and row["sent_at"] is None
    assert not engine.dispatch_one(now + 1)
    store.approve_action(row["action_id"], now + 1)
    assert engine.dispatch_one(now + 2)
    assert len(platform.sent) == 1
    assert len([op for op, _ in model.calls if op == "review_action"]) == 1


def test_model_approval_does_not_override_lost_mutual_relationship(factory, now):
    engine, store, model, platform, access = chat_engine(factory, model_review=True)
    review_model(model, callback=lambda: setattr(access, "allowed", False))
    engine.ingest(private_event(now))
    engine.process_one_event(now)
    engine.dispatch_one(now)
    assert not platform.sent and store.inspect("actions")[0]["state"] == "failed"


def test_human_approval_does_not_override_private_mutual_gate(factory, now):
    engine, store, model, platform, access = chat_engine(factory, model_review=True)
    review_model(model, "reject")
    engine.ingest(private_event(now))
    engine.process_one_event(now)
    engine.dispatch_one(now)
    row = store.inspect("actions")[0]
    store.approve_action(row["action_id"], now + 1)
    access.allowed = False
    engine.dispatch_one(now + 2)
    assert not platform.sent and store.inspect("actions")[0]["state"] == "failed"


def test_private_review_context_never_enters_public_review(factory, now):
    engine, store, model, platform = factory(model_review=True)
    review_model(model)
    store.add_memory("owner", "person", "owner_private", "不能公开的私事", "private-origin", now)
    engine.ingest(inbound(now))
    engine.process_one_event(now)
    engine.dispatch_one(now)
    payload = [p for op, p in model.calls if op == "review_action"][0]
    assert payload["channel"] == "public" and payload["intent_result"] is None
    assert "不能公开的私事" not in str(payload)


def test_cancel_during_review_still_blocks_final_send(factory, now):
    engine, store, model, platform = factory(model_review=True)
    engine.ingest(inbound(now))
    engine.process_one_event(now)
    review_model(model, callback=lambda: store.set_setting("paused", True))
    engine.dispatch_one(now)
    assert not platform.sent and store.inspect("actions")[0]["state"] == "failed"
