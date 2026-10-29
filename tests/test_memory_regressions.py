from dataclasses import replace
import json

import pytest

from murasamebot.adapters.persistence.sqlite import SQLiteStore
from murasamebot.domain.models import Action, Event, PolicyError
from test_memory import archive


@pytest.mark.parametrize("product_kind",["summary","fact","diary","reflection"])
def test_identical_rebuild_creates_active_version_and_retains_inactive_history(factory,now,product_kind):
    e,s,m,p=factory()
    archive(s,now,count=3)
    records=s.memory_records("unlinked:wechat:peer","conversation:wechat:room","private_dialogue",limit=10)
    kind="diary" if product_kind in {"diary","reflection"} else "summary"
    products=[{"kind":product_kind,"text":"用户聊了日常。","sources":[r["record_id"] for r in records]}]
    assert s.memory_commit(records,products,kind,0,now+4)
    old=s.inspect("memory_products")[0]
    s.memory_rebuild(old["person_id"],old["scope"],old["purpose"])
    epoch=s.get_setting("memory_epoch",0)
    assert not s.memory_commit(records,products,kind,0,now+5)
    assert s.memory_commit(records,products,kind,epoch,now+6)
    rows=s.inspect("memory_products")
    active=[r for r in rows if r["active"]]
    assert len(rows)==2 and len(active)==1
    assert active[0]["text"]==old["text"] and active[0]["product_id"]!=old["product_id"]
    assert not next(r for r in rows if r["product_id"]==old["product_id"])["active"]
    assert {r["record_id"] for r in s.memory_product_source_records(active[0]["product_id"])}=={r["record_id"] for r in records}
    assert not s.memory_commit(records,products,kind,epoch,now+7)
    restarted=SQLiteStore(s.path,s.agent_id)
    assert restarted.memory_status()["memory_products"]==1
    s.forget_source(records[0]["event_id"])
    assert not s.memory_commit(records,products,kind,s.get_setting("memory_epoch"),now+8)
    assert all(not r["active"] for r in s.inspect("memory_products"))


def preference(s,now):
    event=Event("owner-interest","wechat","robot","wx-owner","owner-chat","chat","我喜欢摄影",now,"private")
    s.enqueue_event(event);s.finish_event(event.event_id,"done")
    mid=s.add_memory("owner","person","owner_video_recommendation","摄影",event.event_id,now)
    return event,mid


@pytest.mark.parametrize("change",["forget","correction"])
def test_normal_recommendation_tracks_preference_deletion_and_correction(factory,now,change):
    e,s,m,p=factory()
    event,mid=preference(s,now)
    assert e._evaluate(p.read(p.ref),now+1)
    action=Action.from_dict(json.loads(s.inspect("actions")[0]["payload"]))
    payload=next(payload for op,payload in m.calls if op=="evaluate_video")
    assert payload["owner_preferences"] and payload["memory_epoch"]==0
    assert action.memory_epoch==0
    if change=="forget":s.forget(mid)
    else:
        # An explicit correction to the same source invalidates all dependent output.
        records=s.memory_records("owner","conversation:wechat:owner-chat","private_dialogue",limit=10)
        assert s.memory_commit(records,[{"kind":"fact","text":"用户喜欢摄影。","sources":[records[0]["record_id"]]}],"summary",0,now+2)
        s.memory_edit(s.inspect("memory_products")[0]["product_id"],text="更正：用户现在喜欢料理。",now=now+3)
    assert s.validate_action(action,now+4)=="memory_sources_invalidated"
    assert s.inspect("actions")[0]["state"]=="cancelled"
    e.store=SQLiteStore(s.path,s.agent_id)
    assert not e.dispatch_one(now+5) and not p.sent
    with pytest.raises(ValueError):e.queue_manual_send(action.action_id,now+5)


def test_preference_deleted_during_generation_cannot_enqueue_any_derived_actions(factory,now):
    e,s,m,p=factory(video_like=True,video_coin=True)
    event,mid=preference(s,now)
    m.evaluation.update(interested=True,comment_reply={"reply_to":"700","text":"吾辈也觉得这般练习颇好。","topic_class":"casual"})
    original=m.decide
    def decide(op,payload):
        result=original(op,payload)
        if op=="evaluate_video":s.forget(mid)
        return result
    m.decide=decide
    with pytest.raises(PolicyError):e._evaluate(p.read(p.ref),now+1)
    assert not s.inspect("actions") and not p.sent


def test_all_actions_from_one_video_decision_inherit_its_memory_revision(factory,now):
    e,s,m,p=factory(video_like=True,video_coin=True)
    event,mid=preference(s,now)
    m.evaluation.update(interested=True,comment_reply={"reply_to":"700","text":"吾辈也觉得这般练习颇好。","topic_class":"casual"})
    assert e._evaluate(p.read(p.ref),now+1)
    rows=s.inspect("actions")
    assert {r["kind"] for r in rows}=={"recommendation","browse_reply","like","coin"}
    assert all(json.loads(r["payload"])["memory_epoch"]==0 for r in rows)
    s.forget(mid)
    assert all(r["state"]=="cancelled" for r in s.inspect("actions"))


@pytest.mark.parametrize("kind",["recommendation","browse_comment","browse_reply","like","coin"])
def test_legacy_recommendation_without_revision_cannot_be_sent_or_manually_reactivated(factory,now,kind):
    e,s,m,p=factory()
    a=Action("legacy",s.agent_id,"bilibili","robot",p.ref,"吾辈觉得主人会喜欢。",now,now+60,
             kind=kind,mentions=("owner",),binding_version=s.binding("owner","bilibili")["version"],topic_class="casual",proactive=True)
    # Reproduce an already-persisted pre-upgrade row rather than a new enqueue.
    valid=replace(a,memory_epoch=0)
    assert s.enqueue_action(valid,draft=False,now=now)
    with s.connection() as db:db.execute("UPDATE actions SET payload=? WHERE action_id=?",(json.dumps(a.to_dict()),a.action_id))
    assert s.validate_action(a,now+1)=="memory_revision_missing"
    with s.connection() as db:db.execute("UPDATE actions SET state='failed',receipt=? WHERE action_id=?",(json.dumps({"state":"failed","detail":"历史确定失败"}),a.action_id))
    with pytest.raises(ValueError,match="memory_revision_missing"):e.queue_manual_send(a.action_id,now+1)
    with s.connection() as db:db.execute("UPDATE actions SET state='queued' WHERE action_id=?",(a.action_id,))
    assert not e.dispatch_one(now+1) and not p.sent


def test_forgetting_after_dispatch_claim_is_rechecked_before_platform_write(factory,now):
    e,s,m,p=factory()
    event,mid=preference(s,now)
    e._evaluate(p.read(p.ref),now+1)
    original=p.send
    def send(action,mentions,*,before_write=None):
        s.forget(mid)
        return original(action,mentions,before_write=before_write)
    p.send=send
    assert e.dispatch_one(now+2)
    assert not p.sent and s.inspect("actions")[0]["state"]=="failed"
