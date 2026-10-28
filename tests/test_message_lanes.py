from dataclasses import replace

from murasamebot.domain.models import Action, Event, ModelError


def mention(now, platform, *, key="first", actor="101", conversation="same-thread"):
    return Event(key, "bilibili", "robot", actor, conversation, "mention", key, now,
                 content=platform.ref, reply_to=key, thread_root="root")


def test_failed_speaker_does_not_delay_another_speaker_in_the_same_thread(factory, now):
    engine, store, model, platform = factory(min_send_gap_seconds=5, event_retry_seconds=60)
    first = mention(now, platform)
    second = mention(now, platform, key="second", actor="202")
    engine.ingest(first)
    engine.ingest(second)
    original = model.decide
    def decide(operation, payload):
        if operation == "reply" and payload["message"] == "first":
            raise ModelError("模型返回空正文")
        return original(operation, payload)
    model.decide = decide
    assert engine.process_one_event(now)
    assert engine.process_one_event(now + 1)
    assert engine.dispatch_one(now + 1)
    assert platform.sent[0][0].event_id == second.event_id
    assert store.inspect("actions")[0]["state"] == "accepted"
    rows = {r["event_id"]: r for r in store.inspect("events")}
    assert rows["first"]["state"] == "pending"
    assert rows["first"]["next_at"] == now + 60
    assert rows["second"]["state"] == "done"


def test_public_lanes_keep_one_speakers_order_and_release_other_threads(factory, now):
    _, store, _, platform = factory()
    first = mention(now, platform)
    same = replace(first, event_id="same-speaker-next")
    other = replace(first, event_id="other-thread", conversation_id="other-thread")
    for event in [first, same, other]:
        store.enqueue_event(event)
    assert store.claim_event(now).event_id == first.event_id
    store.retry_event(first.event_id, now, "暂时失败", 3)
    assert store.claim_event(now + 1).event_id == other.event_id
    assert store.claim_event(now + 59) is None
    assert store.claim_event(now + 60).event_id == first.event_id
    store.finish_event(first.event_id, "done")
    assert store.claim_event(now + 60).event_id == same.event_id


def test_generation_can_overlap_for_different_speakers_in_one_public_thread(factory, now):
    _, store, _, platform = factory()
    first = mention(now, platform)
    second = replace(first, event_id="second", actor_id="202")
    store.enqueue_event(first)
    store.enqueue_event(second)
    assert store.claim_event(now).event_id == first.event_id
    assert store.claim_event(now).event_id == second.event_id


def test_retry_interval_is_fixed_and_exhaustion_releases_the_next_message(factory, now):
    _, store, _, platform = factory()
    first = mention(now, platform)
    second = replace(first, event_id="second")
    store.enqueue_event(first)
    store.enqueue_event(second)
    for attempt in range(3):
        clock = now + attempt * 60
        assert store.claim_event(clock).event_id == first.event_id
        store.retry_event(first.event_id, clock, "模型暂时失败", 3, delay_seconds=60)
        row = next(r for r in store.inspect("events") if r["event_id"] == first.event_id)
        assert row["next_at"] == clock + 60
        assert row["attempts"] == attempt + 1
    assert row["state"] == "failed"
    assert store.claim_event(now + 120).event_id == second.event_id


def test_outbox_can_send_other_speaker_while_first_speaker_waits_for_review(factory, now):
    engine, store, _, platform = factory(model_review=True, min_send_gap_seconds=5)
    for key, actor in [("first", "101"), ("second", "202")]:
        event = mention(now, platform, key=key, actor=actor)
        engine.ingest(event)
        store.enqueue_action(Action(key, store.agent_id, "bilibili", "robot", platform.ref, "吾辈来聊一聊。",
                                    now, now + 300, kind="reply", event_id=event.event_id,
                                    conversation_id=event.conversation_id, reply_to=event.reply_to,
                                    review_state="passed" if key == "second" else "", review_method="independent"),
                             draft=False, now=now)
    assert engine.dispatch_one(now, pre_reviewed_only=True)
    assert platform.sent[0][0].event_id == "second"
    assert next(r for r in store.inspect("actions") if r["action_id"] == "first")["state"] == "queued"


def test_outbox_keeps_same_speakers_order_with_authoritative_event_sources(factory, now):
    engine, store, _, platform = factory(model_review=True)
    for key in ["first", "second"]:
        event = mention(now, platform, key=key)
        engine.ingest(event)
        store.enqueue_action(Action(key, store.agent_id, "bilibili", "robot", platform.ref, "吾辈来聊一聊。",
                                    now, now + 300, kind="reply", event_id=event.event_id,
                                    conversation_id=event.conversation_id, reply_to=event.reply_to,
                                    review_state="passed" if key == "second" else "", review_method="independent"),
                             draft=False, now=now)
    assert not engine.dispatch_one(now, pre_reviewed_only=True)
    assert not platform.sent


def test_five_second_send_interval_is_shared_by_successful_independent_replies(factory, now):
    engine, store, _, platform = factory(min_send_gap_seconds=5)
    for key, actor in [("first", "101"), ("second", "202")]:
        engine.ingest(mention(now, platform, key=key, actor=actor))
        assert engine.process_one_event(now)
    assert engine.dispatch_one(now)
    assert not engine.dispatch_one(now + 4.99)
    assert engine.dispatch_one(now + 5)
    assert len(platform.sent) == 2


def test_dashboard_queue_counts_distinguish_retry_ordering_and_ready_work(factory, now):
    _, store, _, platform = factory()
    first = mention(now, platform)
    store.enqueue_event(first)
    assert store.claim_event(now)
    store.retry_event(first.event_id, now, "暂时失败", 3)
    for event in [replace(first, event_id="same-speaker"), replace(first, event_id="other", actor_id="202"),
                  replace(first, event_id="ready", actor_id="303")]:
        store.enqueue_event(event)
    assert store.claim_event(now).event_id == "other"
    queues = store.queue_status(now=now + 1)
    assert queues["pending_events"] == 3
    assert queues["processing_events"] == 1
    assert queues["retry_wait_events"] == queues["blocked_events"] == queues["ready_events"] == 1
    assert queues["next_retry_at"] == now + 60


def test_retry_and_send_defaults_are_configurable_from_the_dashboard(tmp_path):
    from murasamebot.bootstrap.config import Config
    from test_architecture_and_config import write_config
    path = write_config(tmp_path)
    config = Config(path)
    assert config.behaviour("robot").event_retry_seconds == 60
    assert config.behaviour("robot").min_send_gap_seconds == 10
    config.save_local({"limits": {"event_retry_seconds": 90, "min_send_gap_seconds": 7}})
    settings = Config(path).behaviour("robot")
    assert settings.event_retry_seconds == 90 and settings.min_send_gap_seconds == 7
