from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from http.server import ThreadingHTTPServer
import json
from threading import Thread
from types import SimpleNamespace

import httpx
import pytest

from murasamebot.bootstrap.web import handler_for
from murasamebot.domain.models import Action, ContentRef, Receipt
from test_architecture_and_config import write_config


def message(engine, now, **changes):
    return replace(Action("manual-message",engine.settings.agent_id,"bilibili","robot",
        ContentRef("bilibili","video","10001"),"吾辈在这儿，慢慢说便好。",now,now+30,
        topic_class="casual"),**changes)


def test_manual_draft_approval_renews_delivery_window_and_still_uses_outbox(factory,now):
    e,s,m,p=factory(draft=True)
    a=message(e,now,review_state="rejected",review_reason="交给人工",memory_epoch=s.get_setting("memory_epoch",0))
    s.enqueue_action(a,draft=True,now=now)
    candidate=e.queue_manual_send(a.action_id,now+60)
    assert candidate.approved_manually and candidate.manual_attempts==1
    assert candidate.text==a.text and candidate.memory_epoch==a.memory_epoch
    assert candidate.expires_at==now+60+e.settings.action_ttl_minutes*60
    assert not p.sent and not m.calls
    assert e.dispatch_one(now+60)
    assert s.inspect("actions")[0]["state"]=="accepted" and len(p.sent)==1


def test_confirmed_failure_requeues_same_message_once_and_keeps_failure_audit(factory,now):
    e,s,m,p=factory()
    a=message(e,now)
    s.enqueue_action(a,draft=False,now=now)
    p.receipt=Receipt("failed",detail="模拟确定失败")
    e.dispatch_one(now)
    assert s.inspect("actions")[0]["state"]=="failed"
    with ThreadPoolExecutor(max_workers=2) as pool:
        def retry(_):
            try:e.queue_manual_send(a.action_id,now+5);return True
            except ValueError:return False
        assert sorted(pool.map(retry,range(2)))==[False,True]
    rows=s.inspect("actions")
    assert len(rows)==1 and rows[0]["action_id"]==a.action_id
    audit=next(r for r in s.inspect("audit") if r["kind"]=="manual_send")
    assert json.loads(audit["detail"])["previous_receipt"]["detail"]=="模拟确定失败"
    p.receipt=Receipt("accepted","new-id")
    assert e.dispatch_one(now+5) and s.inspect("actions")[0]["state"]=="accepted"


@pytest.mark.parametrize("state",["accepted","unknown","sending","queued","cancelled","expired"])
def test_manual_send_rejects_already_sent_unknown_inflight_and_other_states(factory,now,state):
    e,s,m,p=factory()
    a=message(e,now);s.enqueue_action(a,draft=True,now=now)
    with s.connection() as db:db.execute("UPDATE actions SET state=? WHERE action_id=?",(state,a.action_id))
    with pytest.raises(ValueError):e.queue_manual_send(a.action_id,now+1)
    assert not p.sent


@pytest.mark.parametrize("reason",["paused","forgotten","binding","policy","account"])
def test_manual_send_does_not_refresh_permissions_or_memory(factory,now,reason):
    e,s,m,p=factory()
    a=message(e,now,memory_epoch=s.get_setting("memory_epoch",0))
    if reason=="binding":a=replace(a,subject_person_id="owner",binding_version=s.binding("owner","bilibili")["version"])
    s.enqueue_action(a,draft=True,now=now)
    if reason=="paused":s.set_setting("paused",True)
    elif reason=="forgotten":
        with s.transaction() as db:s._bump_memory_epoch(db)
    elif reason=="binding":s.revoke_binding("owner","bilibili")
    elif reason=="policy":e.action_policy=lambda a,t:"公开话题限制"
    elif reason=="account":e.settings=replace(e.settings,account_id="another-account")
    with pytest.raises(ValueError):e.queue_manual_send(a.action_id,now+1)
    assert not p.sent


def test_manual_send_obeys_send_quota_and_interval(factory,now):
    e,s,m,p=factory(daily_comments=1)
    first=message(e,now);s.enqueue_action(first,draft=False,now=now);e.dispatch_one(now)
    second=replace(first,action_id="other",text="吾辈稍后再来。")
    s.enqueue_action(second,draft=True,now=now+1);e.queue_manual_send(second.action_id,now+2)
    assert not e.dispatch_one(now+2) and len(p.sent)==1


def test_repeated_manual_failure_cannot_bypass_minimum_interval(factory,now):
    e,s,m,p=factory(min_send_gap_seconds=10)
    a=message(e,now);s.enqueue_action(a,draft=False,now=now)
    p.receipt=Receipt("failed",detail="确定失败")
    assert e.dispatch_one(now)
    retried=e.queue_manual_send(a.action_id,now+1)
    assert retried.retry_at==now+10
    assert not e.dispatch_one(now+1)
    assert e.dispatch_one(now+10)


def test_manual_retry_does_not_duplicate_a_later_accepted_comment_on_same_content(factory,now):
    e,s,m,p=factory()
    a=message(e,now,kind="owner_comment")
    s.enqueue_action(a,draft=False,now=now)
    p.receipt=Receipt("failed",detail="确定失败");e.dispatch_one(now)
    later=replace(a,action_id="later-comment",kind="browse_comment",text="这期内容甚是有趣。")
    assert s.enqueue_action(later,draft=False,now=now+2)
    p.receipt=Receipt("accepted","later-id");e.dispatch_one(now+2)
    with pytest.raises(ValueError,match="重复入队"):e.queue_manual_send(a.action_id,now+5)


def test_manual_private_send_rechecks_mutual_follow_at_dispatch(factory,now):
    from test_private_messages import chat_engine, private_event
    e,s,m,p,access=chat_engine(factory,draft=True)
    e.ingest(private_event(now));e.process_one_event(now)
    a=Action.from_dict(json.loads(s.inspect("actions")[0]["payload"]))
    e.queue_manual_send(a.action_id,now+1)
    access.allowed=False
    assert e.dispatch_one(now+1)
    assert not p.sent and s.inspect("actions")[0]["state"]=="failed"


@pytest.mark.parametrize("cancelled",[False,True])
def test_failed_task_delivery_can_retry_but_cancellation_cannot_be_reversed(factory,now,cancelled):
    from conftest import queue_recommendation
    e,s,m,p=factory()
    action_id=queue_recommendation(e,s,now)
    p.receipt=Receipt("failed",detail="模拟确定投递失败")
    e.dispatch_one(now)
    task=s.inspect("tasks")[0]
    assert task["state"]=="failed"
    if cancelled:
        s.cancel_task(task["task_id"])
        with pytest.raises(ValueError):e.queue_manual_send(action_id,now+5)
    else:
        e.queue_manual_send(action_id,now+5)
        assert s.inspect("tasks")[0]["state"]=="awaiting_approval"
        p.receipt=Receipt("accepted","final-id")
        assert e.dispatch_one(now+5)
        assert s.inspect("tasks")[0]["state"]=="completed"


def test_manual_send_endpoint_is_authenticated_id_only_and_never_writes_platform(factory,now,tmp_path,monkeypatch):
    import murasamebot.bootstrap.web as web
    e,s,m,p=factory();a=message(e,now);s.enqueue_action(a,draft=True,now=now)
    path=write_config(tmp_path)
    monkeypatch.setattr(web,"build",lambda c:SimpleNamespace(engine=e,close=lambda:None))
    server=ThreadingHTTPServer(("127.0.0.1",0),handler_for(path,"local-token"))
    thread=Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}",trust_env=False) as client:
            headers={"Authorization":"Bearer local-token"}
            assert client.post("/api/actions/send",json={"id":a.action_id}).status_code==401
            assert client.post("/api/actions/send",json={"id":a.action_id},headers={**headers,"Origin":"https://evil.invalid"}).status_code==401
            assert client.post("/api/actions/send",json={"id":a.action_id,"recipient_id":"other"},headers=headers).status_code==400
            response=client.post("/api/actions/send",json={"id":a.action_id},headers=headers)
            assert response.status_code==200 and response.json()["state"]=="queued"
            assert client.post("/api/actions/send",json={"id":a.action_id},headers=headers).status_code==400
            assert not p.sent and not m.calls
    finally:
        server.shutdown();server.server_close();thread.join(timeout=3)
