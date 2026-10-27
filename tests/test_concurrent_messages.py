from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from threading import Barrier, Lock, Event as Signal
import time

import httpx
import pytest

from murasamebot.adapters.models.chat_completions import ChatCompletionsModel
from murasamebot.adapters.models.concurrency import ModelConcurrency
from murasamebot.adapters.platforms.bilibili import BilibiliAdapter
from murasamebot.domain.models import Action, Event, Receipt
from test_bilibili_contracts import credentials
from test_model_review import inbound, review_model


def test_different_conversations_generate_at_the_same_time(factory, now):
    engine, store, model, platform = factory()
    barrier = Barrier(2)
    original = model.decide
    def decide(op, payload):
        if op == "reply":
            barrier.wait(timeout=5)
        return original(op, payload)
    model.decide = decide
    first = inbound(now)
    engine.ingest(first)
    engine.ingest(replace(first, event_id="other", conversation_id="other-conversation"))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: engine.process_one_event(now), range(2)))
    assert results == [True, True]
    assert len(store.inspect("actions")) == 2


def test_same_conversation_cannot_be_claimed_concurrently(factory, now):
    engine, store, _, _ = factory()
    first = inbound(now)
    engine.ingest(first)
    engine.ingest(replace(first, event_id="second"))
    engine.ingest(replace(first, event_id="third", conversation_id="other"))
    assert store.claim_event(now).event_id == first.event_id
    assert store.claim_event(now).event_id == "third"
    assert store.claim_event(now) is None
    store.finish_event(first.event_id, "done")
    assert store.claim_event(now).event_id == "second"


def test_linked_private_identity_serializes_intent_across_platforms(factory, now):
    engine, store, _, _ = factory()
    first = Event("wx", "wechat", "bot", "wx-owner", "wx-conv", "chat", "找视频", now, visibility="private")
    second = Event("bili", "bilibili", "robot", "424242", "bili-conv", "private_message", "更正需求", now, visibility="private")
    engine.ingest(first); engine.ingest(second)
    assert store.claim_event(now).event_id == "wx"
    assert store.claim_event(now) is None
    store.finish_event("wx", "done")
    assert store.claim_event(now).event_id == "bili"


def test_model_reviews_can_overlap_and_sender_waits_for_pass(factory, now):
    engine, store, model, platform = factory(model_review=True)
    first = inbound(now)
    engine.ingest(first)
    engine.ingest(replace(first, event_id="other", conversation_id="other"))
    engine.process_one_event(now); engine.process_one_event(now)
    assert not engine.dispatch_one(now, pre_reviewed_only=True)
    barrier = Barrier(2)
    review_model(model, callback=lambda: barrier.wait(timeout=5))
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(lambda _: engine.review_one(now), range(2))) == [True, True]
    assert not platform.sent
    assert all(json.loads(row["payload"])["review_state"] == "passed" for row in store.inspect("actions"))
    assert engine.dispatch_one(now, pre_reviewed_only=True)
    assert len([op for op, _ in model.calls if op == "review_action"]) == 2


def test_failed_parallel_review_goes_to_draft(factory, now):
    engine, store, model, platform = factory(model_review=True)
    engine.ingest(inbound(now)); engine.process_one_event(now)
    review_model(model, "reject")
    assert engine.review_one(now)
    assert store.inspect("actions")[0]["state"] == "draft"
    assert not engine.dispatch_one(now, pre_reviewed_only=True)


def test_review_recovery_cannot_result_in_automatic_send(factory, now):
    engine, store, model, platform = factory(model_review=True)
    engine.ingest(inbound(now)); engine.process_one_event(now)
    assert store.claim_review_action(now)
    assert store.claim_review_action(now + 901) is None
    assert store.inspect("actions")[0]["state"] == "draft"
    assert not engine.dispatch_one(now + 901, pre_reviewed_only=True)


def test_later_review_cannot_overtake_earlier_reply(factory, now):
    engine, store, _, platform = factory(model_review=True)
    first = Action("one", store.agent_id, "bilibili", "robot", platform.ref, "吾辈先回这一句。", now, now+300,
                   conversation_id="same", kind="reply")
    second = replace(first, action_id="two", review_state="passed")
    store.enqueue_action(first, draft=False, now=now)
    store.enqueue_action(second, draft=False, now=now)
    assert not engine.dispatch_one(now, pre_reviewed_only=True)
    assert not platform.sent


def test_single_key_performs_parallel_requests_under_shared_limit(factory):
    _, store, _, _ = factory()
    guard, lock = ModelConcurrency(2), Lock()
    active, peak = 0, 0
    def handle(request):
        nonlocal active, peak
        assert request.headers["Authorization"] == "Bearer same-key"
        with lock:
            active += 1; peak = max(peak, active)
        time.sleep(0.08)
        with lock:
            active -= 1
        return httpx.Response(200, json={"choices":[{"finish_reason":"stop","message":{"content":'{"decision":"reply","text":"你好。"}'}}]})
    gateways = [ChatCompletionsModel(base_url="https://model.invalid", name="model", api_key="same-key", store=store,
                timezone="Asia/Hong_Kong", daily_limit=10, concurrency=guard,
                client=httpx.Client(transport=httpx.MockTransport(handle))) for _ in range(4)]
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda g: g.decide("reply", {"message":"你好"}), gateways))
        assert len(results) == 4 and peak == 2
    finally:
        for gateway in gateways:
            gateway.close()


def test_notifications_keep_latest_90_when_both_feeds_overflow(tmp_path):
    pages = {}
    def handle(request):
        if request.url.path.endswith("nav"):
            return httpx.Response(200,json={"code":0,"data":{"isLogin":True,"mid":999}})
        path = request.url.path
        page = pages.get(path, 0); pages[path] = page+1
        base = (0 if path.endswith("reply") else 1000) + page*30
        items = [{"id":i,"at_time":10000+i,"reply_time":10000+i,"user":{"mid":123},
                  "item":{"source_id":i+1,"subject_id":42,"business_id":1,"source_content":"聊一聊"}} for i in range(base,base+30)]
        assert request.url.params["ps"] == "30"
        return httpx.Response(200,json={"code":0,"data":{"items":items,"cursor":{"is_end":False,"id":base+30,"time":10000}}})
    adapter = BilibiliAdapter(credentials(tmp_path),client=httpx.Client(transport=httpx.MockTransport(handle)))
    events = adapter.notifications(12000)
    assert len(events) == 90 and all(e.occurred_at >=11000 for e in events)
    assert set(pages.values()) == {3}
    warnings = adapter.take_warnings()
    assert len(warnings) == 3 and "90" in warnings[-1]["detail"]
    assert adapter.take_warnings() == []


def test_polling_remains_available_while_model_jobs_are_running(factory, now, tmp_path):
    from types import SimpleNamespace
    import yaml
    from murasamebot.bootstrap.config import Config
    from murasamebot.bootstrap.runtime import Runtime
    from murasamebot.bootstrap.workers import WorkerCoordinator
    from test_architecture_and_config import write_config
    engine, store, model, platform = factory()
    path = write_config(tmp_path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data.update(agent_id=store.agent_id, database=str(store.path), mode="auto", processing={"model_concurrency":2})
    path.write_text(yaml.safe_dump(data,allow_unicode=True),encoding="utf-8")
    started, release = Barrier(3), Signal()
    original = model.decide
    def decide(op, payload):
        if op == "reply":
            started.wait(timeout=5)
            assert release.wait(timeout=5)
        return original(op, payload)
    model.decide = decide
    event = inbound(now)
    engine.ingest(event);engine.ingest(replace(event,event_id="second",conversation_id="other"))
    coordinator = WorkerCoordinator(path, runtime_builder=lambda config,**kw:SimpleNamespace(engine=engine,store=store,close=lambda:None))
    try:
        coordinator.pump(Config(path))
        started.wait(timeout=5)
        assert store.queue_status()["processing_events"] == 2
        platform.account_id="robot"
        platform.notifications=lambda stamp:[]
        platform.owner_content=lambda uid,stamp:[]
        Runtime(Config(path),store,platform,model,engine).poll(now)
        assert store.get_setting("notifications_initialized:robot") is True
        release.set()
        assert all(future.result(timeout=5) for future in coordinator.messages)
    finally:
        release.set()
        coordinator.close()


def test_idle_worker_recovers_without_waiting_for_new_messages(factory, now):
    engine, store, model, platform = factory(model_review=True)
    engine.ingest(inbound(now)); engine.process_one_event(now)
    assert store.claim_review_action(now)
    engine.ingest(replace(inbound(now), event_id="interrupted", conversation_id="other"))
    assert store.claim_event(now).event_id == "interrupted"
    store.recover_idle_work(now + 901)
    assert store.inspect("actions")[0]["state"] == "draft"
    assert store.claim_event(now + 901).event_id == "interrupted"


def test_adjusting_model_limit_never_cancels_running_requests():
    gate = ModelConcurrency(2)
    started = [Signal(), Signal(), Signal()]
    release = [Signal(), Signal()]
    def request(index):
        with gate:
            started[index].set()
            if index < 2:
                assert release[index].wait(timeout=5)
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(request, 0)
        second = pool.submit(request, 1)
        assert started[0].wait(timeout=5) and started[1].wait(timeout=5)
        gate.resize(1)
        third = pool.submit(request, 2)
        try:
            release[0].set();first.result(timeout=5)
            assert not started[2].wait(timeout=0.05)
            release[1].set();second.result(timeout=5)
            assert started[2].wait(timeout=5)
            third.result(timeout=5)
        finally:
            release[0].set();release[1].set()
