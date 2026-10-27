from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from murasamebot.adapters.persistence.sqlite import SQLiteStore
from murasamebot.application.scheduling import day_start
from murasamebot.domain.models import Action, ContentRef, Event, Receipt
from conftest import queue_recommendation


def test_legacy_notification_context_is_enriched_without_replaying_or_reviving_deleted_source(factory, now):
    engine, store, model, platform = factory()
    event = Event("legacy-draw", "bilibili", "robot", "123", "thread", "mention", "消息", now,
                  content=ContentRef("bilibili", "draw", "42"))
    store.enqueue_event(event)
    enriched = replace(event, share_content=ContentRef("bilibili", "post", "900"))
    assert not store.enqueue_event(enriched)
    assert store.event(event.event_id).share_content == enriched.share_content
    assert store.inspect("events")[0]["state"] == "pending"
    store.finish_event(event.event_id, "deleted")
    assert not store.enqueue_event(enriched) and store.event(event.event_id) is None


def test_two_workers_cannot_claim_same_action(factory, now):
    engine, store, model, platform = factory()
    queue_recommendation(engine, store, now)
    other = SQLiteStore(store.path, store.agent_id)
    def claim(repository):
        return repository.claim_action(now, 20, 3, day_start(now, "Asia/Hong_Kong"), 0, 0)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, [store, other]))
    assert sum(item is not None for item in results) == 1


def test_crashed_sender_becomes_unknown_not_queued(factory, now):
    engine, store, model, platform = factory()
    queue_recommendation(engine, store, now)
    assert store.claim_action(now, 20, 3, now - 100, 0, 0)
    restarted = SQLiteStore(store.path, store.agent_id)
    assert restarted.claim_action(now + 181, 20, 3, now - 100, 0, 0) is None
    assert restarted.inspect("actions")[0]["state"] == "unknown"
    assert restarted.inspect("tasks")[0]["state"] == "delivery_unknown"


def test_agent_state_isolation(factory, now):
    engine, store, model, platform = factory()
    queue_recommendation(engine, store, now)
    other = SQLiteStore(store.path, "another-agent")
    assert other.inspect("actions") == []
    assert other.inspect("tasks") == []
    assert other.resolve_person("bilibili", "424242") is None
    assert other.memory("owner", "person", "owner_video_recommendation", now) == []


def test_expired_action_cannot_send_even_if_queued(factory, now):
    engine, store, model, platform = factory()
    queue_recommendation(engine, store, now)
    assert not engine.dispatch_one(now + 1801)
    assert store.inspect("actions")[0]["state"] == "expired"


def test_daily_budget_reserved_before_second_worker_can_write(factory, now):
    engine, store, model, platform = factory()
    first = Action("a1", store.agent_id, "bilibili", "robot", ContentRef("bilibili", "video", "1"), "吾辈想聊聊这个。", now, now + 3600, kind="owner_comment")
    second = replace(first, action_id="a2", content=ContentRef("bilibili", "video", "2"))
    store.enqueue_action(first, draft=False, now=now)
    store.enqueue_action(second, draft=False, now=now)
    claimed = store.claim_action(now, 1, 3, now - 100, 0, 0)
    store.finish_action(claimed.action_id, Receipt("accepted", "123"), now)
    assert store.claim_action(now + 1, 1, 3, now - 100, 0, 0) is None


def test_model_budget_is_atomic(factory, now):
    engine, store, model, platform = factory()
    from murasamebot.domain.models import ModelError
    other = SQLiteStore(store.path, store.agent_id)
    def reserve(repository):
        try:
            repository.reserve_model_call("2026-10-26", 1)
            return True
        except ModelError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(reserve, [store, other])) == 1
