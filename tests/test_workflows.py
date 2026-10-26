from dataclasses import replace
import json

import pytest

from murasamebot.adapters.persistence.sqlite import SQLiteStore
from murasamebot.application.engine import SocialEngine
from murasamebot.domain.models import ContentRef, Document, Event, Receipt
from conftest import owner_message, queue_recommendation


def test_private_request_becomes_public_recommendation_without_private_context(factory, now):
    engine, store, model, platform = factory()
    engine.ingest(owner_message(now, text="我最近工作压力很大，今晚想看摄影入门视频"))
    engine.process_one_event(now)
    assert store.inspect("tasks")[0]["state"] == "scheduled"
    engine.run_due_task(now)
    payload = next(payload for operation, payload in model.calls if operation == "evaluate_video")
    assert "工作压力" not in json.dumps(payload, ensure_ascii=False)
    assert payload["requested_topic"] == "摄影入门"
    engine.dispatch_one(now)
    assert platform.sent[0][1] == {"owner": "424242"}
    assert store.inspect("tasks")[0]["state"] == "completed"


def test_restart_keeps_task_and_rejects_duplicate_source(factory, now):
    engine, store, model, platform = factory()
    event = owner_message(now)
    engine.ingest(event)
    engine.process_one_event(now)
    restarted = SQLiteStore(store.path, store.agent_id)
    other = SocialEngine(engine.settings, restarted, model, engine.persona, {"bilibili": platform}, {"bilibili": platform})
    assert not other.ingest(event)
    assert other.run_due_task(now)
    other.dispatch_one(now)
    assert len(platform.sent) == 1
    assert not other.run_due_task(now + 1)


def test_same_name_unlinked_sender_is_not_owner(factory, now):
    engine, store, model, platform = factory()
    event = replace(owner_message(now), actor_id="stranger", actor_name="主人")
    engine.ingest(event)
    engine.process_one_event(now)
    assert not store.inspect("tasks")
    assert all(operation != "extract_intent" for operation, _ in model.calls)
    assert not any(json.loads(row["payload"])["mentions"] for row in store.inspect("actions"))


def test_cancel_invalidates_already_queued_recommendation(factory, now):
    engine, store, model, platform = factory()
    queue_recommendation(engine, store, now)
    model.intent = {"kind": "cancel"}
    engine.ingest(owner_message(now + 1, "cancel", "不用找了"))
    engine.process_one_event(now + 1)
    assert store.inspect("tasks")[0]["state"] == "cancelled"
    assert store.inspect("actions")[0]["state"] == "cancelled"
    assert not engine.dispatch_one(now + 2)
    assert not platform.sent


def test_delete_memory_tombstones_source_and_cancels_output(factory, now):
    engine, store, model, platform = factory()
    queue_recommendation(engine, store, now)
    store.forget(store.inspect("memories")[0]["memory_id"])
    assert not engine.ingest(owner_message(now))
    assert not engine.dispatch_one(now + 1)
    assert store.inspect("events")[0]["state"] == "deleted"
    assert all(row["text"] == "[已删除]" for row in store.inspect("memories"))


def test_binding_revocation_cancels_pending_delivery(factory, now):
    engine, store, model, platform = factory()
    queue_recommendation(engine, store, now)
    store.revoke_binding("owner", "bilibili")
    assert not engine.dispatch_one(now)
    assert not platform.sent


def test_revoking_request_source_binding_also_cancels_cross_platform_delivery(factory, now):
    engine, store, model, platform = factory()
    queue_recommendation(engine, store, now)
    store.revoke_binding("owner", "wechat")
    assert store.inspect("tasks")[0]["state"] == "cancelled"
    assert store.inspect("actions")[0]["state"] == "cancelled"
    assert not engine.dispatch_one(now)


def test_expired_draft_can_be_regenerated_without_duplicate_active_drafts(factory, now):
    engine, store, model, platform = factory(draft=True)
    first = queue_recommendation(engine, store, now)
    assert engine.run_due_task(now + 1801)
    rows = store.inspect("actions")
    assert len(rows) == 2
    assert len([row for row in rows if row["state"] == "draft"]) == 1
    assert [row for row in rows if row["action_id"] == first][0]["state"] == "expired"
    assert not engine.run_due_task(now + 1802)


def test_unknown_send_never_retries_or_completes_task(factory, now):
    engine, store, model, platform = factory()
    queue_recommendation(engine, store, now)
    platform.receipt = Receipt("unknown", detail="连接超时")
    engine.dispatch_one(now)
    assert store.inspect("actions")[0]["state"] == "unknown"
    assert store.inspect("tasks")[0]["state"] == "delivery_unknown"
    assert not engine.dispatch_one(now + 200)
    assert not engine.run_due_task(now + 200)
    assert len(platform.sent) == 1


def test_preference_does_not_schedule_task(factory, now):
    engine, store, model, platform = factory()
    model.intent = {"kind": "preference", "topic": "摄影", "sensitive": False}
    engine.ingest(owner_message(now, text="最近喜欢摄影"))
    engine.process_one_event(now)
    assert not store.inspect("tasks")
    assert store.inspect("memories")[0]["kind"] == "preference"


def test_implicit_wish_cannot_become_recurring_task(factory, now):
    engine, store, model, platform = factory()
    model.intent["interval_days"] = 7
    engine.ingest(owner_message(now))
    engine.process_one_event(now)
    assert store.inspect("tasks")[0]["interval_days"] == 0


def test_recurring_request_reschedules_only_after_confirmed_acceptance(factory, now):
    engine, store, model, platform = factory()
    model.intent["interval_days"] = 7
    engine.ingest(owner_message(now, text="以后每周推荐摄影教程"))
    engine.process_one_event(now)
    engine.run_due_task(now)
    engine.dispatch_one(now)
    task = store.inspect("tasks")[0]
    assert task["state"] == "scheduled" and task["version"] == 2
    assert task["due_at"] == now + 7 * 86400


def test_update_replaces_queued_request_instead_of_sending_old_topic(factory, now):
    engine, store, model, platform = factory()
    queue_recommendation(engine, store, now)
    model.intent = {"kind": "update", "topic": "做饭入门", "sensitive": False}
    engine.ingest(owner_message(now + 1, "update", "改成做饭视频吧"))
    engine.process_one_event(now + 1)
    assert store.active_tasks("owner")[0]["topic"] == "做饭入门"
    assert store.inspect("actions")[0]["state"] == "cancelled"


def test_late_old_request_cannot_revive_after_newer_cancellation(factory, now):
    engine, store, model, platform = factory()
    model.intent = {"kind": "cancel"}
    engine.ingest(owner_message(now + 20, "cancel", "不用再找了"))
    engine.process_one_event(now + 20)
    model.intent = {"kind": "request", "topic": "摄影"}
    engine.ingest(owner_message(now, "late-request"))
    engine.process_one_event(now + 21)
    assert not store.inspect("tasks")


def test_private_memory_is_not_retrieved_for_public_reply(factory, now):
    engine, store, model, platform = factory()
    store.add_memory("owner", "person", "owner_private", "不能公开的私人资料", "private-source", now)
    event = Event("mention", "bilibili", "robot", "424242", "public-thread", "mention", "怎么看这个视频？", now, content=platform.ref, reply_to="700", thread_root="600")
    engine.ingest(event)
    engine.process_one_event(now)
    payload = model.calls[-1][1]
    assert "私人资料" not in json.dumps(payload, ensure_ascii=False)
    action = json.loads(store.inspect("actions")[0]["payload"])
    assert action["reply_to"] == "700" and action["thread_root"] == "600"


def test_title_only_video_is_not_used_for_proactive_recommendation(factory, now):
    engine, store, model, platform = factory()
    platform.documents[platform.ref.key] = Document(platform.ref, "摄影教程", "只有简介", "metadata")
    queue_event = owner_message(now)
    engine.ingest(queue_event)
    engine.process_one_event(now)
    engine.run_due_task(now)
    assert not store.inspect("actions")
    assert not any(operation == "evaluate_video" for operation, _ in model.calls)


def test_model_can_choose_silence_and_cannot_select_unknown_comment(factory, now):
    engine, store, model, platform = factory()
    model.evaluation = {"decision": "skip", "reason": "没有值得补充的观点"}
    assert engine.browse(now) == 1
    assert not store.inspect("actions")
    model.evaluation = {"decision": "reply", "text": "吾辈赞同这个看法。", "reply_to": "not-observed"}
    engine.browse(now + 8 * 86400)
    assert not store.inspect("actions")


def test_draft_requires_approval_but_cancelled_draft_cannot_be_approved(factory, now):
    engine, store, model, platform = factory(draft=True)
    action_id = queue_recommendation(engine, store, now)
    assert not engine.dispatch_one(now)
    store.cancel_task(store.inspect("tasks")[0]["task_id"])
    with pytest.raises(ValueError):
        store.approve_action(action_id, now)


def test_manual_approval_can_send_while_new_actions_remain_drafts(factory, now):
    engine, store, model, platform = factory(draft=True)
    action_id = queue_recommendation(engine, store, now)
    store.approve_action(action_id, now)
    assert engine.dispatch_one(now)
    assert len(platform.sent) == 1


def test_switching_to_review_mode_stops_queued_auto_actions(factory, now):
    engine, store, model, platform = factory()
    queue_recommendation(engine, store, now)
    store.enable_review_mode()
    engine.settings = replace(engine.settings, draft=True)
    assert store.inspect("actions")[0]["state"] == "draft"
    assert not engine.dispatch_one(now)


def test_request_deadline_expires_instead_of_recommending_next_day(factory, now):
    engine, store, model, platform = factory()
    from datetime import datetime
    model.intent["not_after"] = "2026-10-26T23:59:59+08:00"
    engine.ingest(owner_message(now))
    engine.process_one_event(now)
    tomorrow = now + 86400
    engine.run_due_task(tomorrow)
    assert store.inspect("tasks")[0]["state"] == "expired"
    assert not platform.searches


def test_explicit_preference_correction_removes_old_preference(factory, now):
    engine, store, model, platform = factory()
    model.intent = {"kind": "preference", "topic": "摄影"}
    engine.ingest(owner_message(now, "pref1", "我最近喜欢摄影"))
    engine.process_one_event(now)
    previous = store.inspect("memories")[0]["memory_id"]
    model.intent = {"kind": "preference", "topic": "做饭", "forget_memory_ids": [previous]}
    engine.ingest(owner_message(now + 1, "pref2", "现在不喜欢摄影了，改看做饭"))
    engine.process_one_event(now + 1)
    active = store.memory("owner", "person", "owner_video_recommendation", now + 1)
    assert [row["text"] for row in active] == ["做饭"]


def test_pause_prevents_all_queued_writes(factory, now):
    engine, store, model, platform = factory()
    queue_recommendation(engine, store, now)
    store.set_setting("paused", True)
    assert not engine.dispatch_one(now)
    assert not platform.sent


def test_cancel_during_sink_preparation_blocks_final_write(factory, now):
    engine, store, model, platform = factory()
    queue_recommendation(engine, store, now)
    def delayed_send(action, mentions, *, before_write=None):
        store.cancel_task(action.task_id)
        assert before_write is not None and not before_write()
        return Receipt("failed", detail="任务已取消，未发送")
    platform.send = delayed_send
    engine.dispatch_one(now)
    assert store.inspect("tasks")[0]["state"] == "cancelled"
    assert store.inspect("actions")[0]["state"] == "failed"


def test_platform_outage_does_not_block_another_channels_owner_request(factory, now):
    engine, store, model, platform = factory()
    store.set_setting("platform_paused:bilibili", True)
    blocked = Event("bili-reply", "bilibili", "robot", "123", "thread", "reply", "你好", now, content=platform.ref, reply_to="700")
    engine.ingest(blocked)
    engine.ingest(owner_message(now))
    assert engine.process_one_event(now)
    assert store.active_tasks("owner")[0]["topic"] == "摄影入门"
    assert not engine.run_due_task(now)
    assert not platform.sent


def test_identity_binding_without_recommendation_purpose_cannot_create_cross_platform_task(factory, now):
    engine, store, model, platform = factory()
    store.bind("owner", "wechat", "wx-owner", purpose="dialogue_only")
    engine.ingest(owner_message(now))
    engine.process_one_event(now)
    assert not store.inspect("tasks")
    assert all(operation != "extract_intent" for operation, _ in model.calls)


def test_owner_content_and_browse_do_not_double_post_same_video(factory, now):
    engine, store, model, platform = factory()
    platform.documents[platform.ref.key] = replace(platform.read(platform.ref), author_id="424242")
    event = Event("owner-new", "bilibili", "robot", "424242", "owner-post", "owner_content", "新视频", now, content=platform.ref)
    engine.ingest(event)
    engine.process_one_event(now)
    engine.browse(now)
    assert len(store.inspect("actions")) == 1


@pytest.mark.parametrize("text", ["こんにちは", "【高兴】吾辈很喜欢。", "吾辈喜欢。<好きです>", "@陌生人 吾辈喜欢。"])
def test_replies_reject_translation_tags_or_model_selected_mentions(factory, now, text):
    engine, store, model, platform = factory()
    model.reply["text"] = text
    event = Event("reply", "bilibili", "robot", "123", "thread", "reply", "问题", now, content=platform.ref, reply_to="700")
    engine.ingest(event)
    engine.process_one_event(now)
    assert not store.inspect("actions")
