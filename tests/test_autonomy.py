from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace

import httpx
import pytest

from murasamebot.application.autonomy import AutonomySettings
from murasamebot.domain.models import Action, Event, Receipt, PlatformError


def enable(engine, model, *, channel="dynamic", **settings):
    policy = AutonomySettings(**{channel+"_enabled": True, "windows": (("00:00","23:59"),), **settings})
    engine.settings = replace(engine.settings, autonomy=policy, browsing_enabled=False)
    allowed = [True]
    engine.private_access["bilibili"] = SimpleNamespace(allows_private_reply=lambda account, peer: allowed[0],
        private_conversation=lambda account, peer: f"private/{account}/{peer}")
    proposal = {"decision":"send","text":"唔，今日也想聊聊游戏里的小巧思。","delay_minutes":10,
                "can_send":True,"review_reason":"自然的角色问候"}
    original = model.decide
    def decide(operation, payload):
        if operation.startswith("autonomous_"):
            model.calls.append((operation,deepcopy(payload)))
            return deepcopy(proposal)
        return original(operation,payload)
    model.decide = decide
    return proposal, allowed


def queued(store):
    return Action.from_dict(json.loads(store.inspect("actions")[0]["payload"]))


def test_post_plan_survives_restart_waits_until_due_and_archives_only_acceptance(factory, now):
    engine,store,model,platform = factory()
    enable(engine,model)
    assert engine.autonomy.run(now)
    assert not store.inspect("conversation_records")
    action=queued(store)
    assert action.kind=="autonomous_post" and action.not_before==now+600
    assert store.queue_status(now=now+599)["ready_actions"]==0
    assert store.queue_status(now=now+600)["ready_actions"]==1
    assert not engine.dispatch_one(now+599)
    from murasamebot.adapters.persistence.sqlite import SQLiteStore
    engine.store=SQLiteStore(store.path,store.agent_id)
    assert engine.dispatch_one(now+600)
    assert platform.sent[0][0].action_id==action.action_id
    records=store.inspect("conversation_records")
    assert len(records)==1 and records[0]["role"]=="agent" and records[0]["visibility"]=="public"
    assert not engine.autonomy.run(now+3600)
    assert len(store.inspect("actions"))==1


@pytest.mark.parametrize("decision", ["skip","broken"])
def test_skip_or_malformed_decision_never_creates_action(factory,now,decision):
    e,s,m,_=factory();p,_=enable(e,m);p["decision"]=decision
    assert not e.autonomy.run(now) and not s.inspect("actions")
    assert not e.autonomy.run(now+15)
    assert len(m.calls)==1


@pytest.mark.parametrize("delay", [-1,True,"10",121,float("nan")])
def test_untrusted_model_schedule_is_bounded(factory,now,delay):
    e,s,m,_=factory();p,_=enable(e,m);p["delay_minutes"]=delay
    assert not e.autonomy.run(now) and not s.inspect("actions")


def test_rejected_inline_judgement_stays_in_drafts_and_pending_blocks_new_plans(factory,now):
    e,s,m,_=factory(model_review=True,review_strategy="inline");p,_=enable(e,m);p["can_send"]=False
    assert e.autonomy.run(now)
    assert s.inspect("actions")[0]["state"]=="draft"
    assert not e.dispatch_one(now+600,pre_reviewed_only=True)
    assert not e.autonomy.run(now+1800)
    assert len(m.calls)==1


def test_public_post_never_receives_owner_private_persona_or_private_history(factory,now):
    e,s,m,_=factory();enable(e,m)
    e.persona=replace(e.persona,private_owner_context="私人关系秘密")
    e.ingest(Event("secret","bilibili","robot","424242","private/robot/424242","private_message","主人私事",now-7200,"private"))
    assert e.autonomy.run(now)
    payload=next(payload for operation,payload in m.calls if operation=="autonomous_post")
    assert "主人私事" not in json.dumps(payload,ensure_ascii=False)
    assert "私人关系秘密" not in payload["persona"]
    assert payload["history"]==[] and payload["memories"]==[]


def test_proactive_private_is_bound_to_owner_not_a_model_recipient(factory,now):
    e,s,m,p=factory();proposal,_=enable(e,m,channel="private")
    proposal["recipient_id"]="stranger"
    e.persona=replace(e.persona,private_owner_context="只给主人的设定")
    assert e.autonomy.run(now)
    a=queued(s)
    assert a.recipient_id=="424242" and not a.event_id and a.visibility=="private"
    assert e.dispatch_one(now+600)
    assert p.sent[0][0].recipient_id=="424242"
    record=s.inspect("conversation_records")[0]
    assert record["role"]=="agent" and record["person_id"]=="owner" and record["purpose"]=="private_dialogue"
    assert not any(r["role"]=="user" for r in s.inspect("conversation_records"))


def test_unfollow_before_send_blocks_proactive_private(factory,now):
    e,s,m,p=factory();_,allowed=enable(e,m,channel="private")
    assert e.autonomy.run(now);allowed[0]=False
    assert e.dispatch_one(now+600) and not p.sent
    assert s.inspect("actions")[0]["state"]=="failed"


def test_nonmutual_owner_never_triggers_model(factory,now):
    e,s,m,_=factory();_,allowed=enable(e,m,channel="private");allowed[0]=False
    assert not e.autonomy.run(now) and not m.calls and not s.inspect("actions")


def test_new_owner_message_cancels_stale_scheduled_opening(factory,now):
    e,s,m,p=factory();enable(e,m,channel="private")
    assert e.autonomy.run(now)
    e.ingest(Event("new","bilibili","robot","424242","private/robot/424242","private_message","我有个问题",now+1,"private"))
    assert e.dispatch_one(now+600) and not p.sent
    assert s.inspect("actions")[0]["state"]=="failed"


def test_pending_or_recent_conversation_prevents_active_opening(factory,now):
    e,s,m,_=factory();enable(e,m,channel="private")
    e.ingest(Event("busy","bilibili","robot","424242","private/robot/424242","private_message","聊聊",now-10,"private"))
    assert not e.autonomy.run(now) and not m.calls
    s.finish_event("busy","done")
    assert not e.autonomy.run(now+1800) and not m.calls


def test_revoked_binding_and_forgotten_memory_invalidate_active_opening(factory,now):
    e,s,m,p=factory();enable(e,m,channel="private")
    assert e.autonomy.run(now)
    s.revoke_binding("owner","bilibili")
    assert not e.dispatch_one(now+600) and not p.sent


def test_unknown_send_does_not_automatically_generate_another_message(factory,now):
    e,s,m,p=factory();enable(e,m);p.receipt=Receipt("unknown",detail="不确定是否已发布")
    assert e.autonomy.run(now) and e.dispatch_one(now+600)
    assert not e.autonomy.run(now+86400)
    assert len(p.sent)==1 and len(m.calls)==1


def test_global_pause_disabled_and_closed_window_do_not_call_model(factory,now):
    e,s,m,_=factory()
    assert not e.autonomy.run(now) and not m.calls
    enable(e,m,windows=(("01:00","02:00"),))
    assert not e.autonomy.run(now)
    enable(e,m);s.set_setting("paused",True)
    assert not e.autonomy.run(now) and not m.calls


def test_decision_budget_limits_skips_as_well_as_sends(factory,now):
    e,s,m,_=factory();p,_=enable(e,m,daily_decision_limit=1);p["decision"]="skip"
    assert not e.autonomy.run(now)
    assert not e.autonomy.run(now+1800)
    assert len(m.calls)==1


def test_settings_changes_cancel_old_drafts_even_if_reenabled_later(tmp_path,now):
    from test_architecture_and_config import write_config
    from murasamebot.bootstrap.config import Config
    from murasamebot.bootstrap.runtime import prepare_store
    path=write_config(tmp_path);cfg=Config(path);s=prepare_store(cfg)
    policy=cfg.behaviour().autonomy
    a=Action("old",s.agent_id,"bilibili","999",None,"吾辈想说句话。",now,now+3600,
             kind="autonomous_post",not_before=now,autonomy_version=policy.version())
    assert s.enqueue_action(a,draft=True,now=now)
    cfg.save_local({"autonomy":{"dynamic_enabled":True}})
    prepare_store(Config(path))
    assert s.inspect("actions")[0]["state"]=="cancelled"


def test_adapter_creates_text_dynamic_without_artwork_or_source_reference(tmp_path):
    from test_bilibili_contracts import credentials,nav_data
    from murasamebot.adapters.platforms.bilibili import BilibiliAdapter
    writes=[]
    def handle(request):
        if request.method=="GET":return httpx.Response(200,json={"code":0,"data":nav_data()})
        writes.append(request);body=json.loads(request.content)
        assert body["dyn_req"]["scene"]==1 and "pics" not in body["dyn_req"]
        assert body["dyn_req"]["content"]["contents"][0]["raw_text"]=="吾辈今日有个小想法。"
        return httpx.Response(200,json={"code":0,"data":{"dyn_id_str":"1234"}})
    bot=BilibiliAdapter(credentials(tmp_path),client=httpx.Client(transport=httpx.MockTransport(handle)))
    a=Action("post","agent","bilibili","999",None,"吾辈今日有个小想法。",1,2,kind="autonomous_post")
    assert bot.send(a,{}).state=="accepted" and len(writes)==1


def test_adapter_proactive_private_uses_only_configured_mutual_owner(tmp_path):
    from test_bilibili_contracts import credentials,nav_data
    from murasamebot.adapters.platforms.bilibili import BilibiliAdapter
    posts=[]
    def handle(request):
        if request.url.path.endswith('/nav'):return httpx.Response(200,json={"code":0,"data":nav_data()})
        if request.url.path.endswith('/relation'):return httpx.Response(200,json={"code":0,"data":{"mid":123,"attribute":6}})
        if request.url.path.endswith('/fetch_session_msgs'):return httpx.Response(200,json={"code":0,"data":{"messages":[]}})
        assert request.method=='POST';posts.append(request)
        return httpx.Response(200,json={"code":0,"data":{"msg_key":"777"}})
    bot=BilibiliAdapter(credentials(tmp_path),proactive_recipient_id="123",client=httpx.Client(transport=httpx.MockTransport(handle)))
    a=Action("dm","agent","bilibili","999",None,"主人今日可好？",1,2,kind="proactive_private",visibility="private",recipient_id="123",conversation_id="bilibili:999:private:123")
    assert bot.send(a,{}).state=='accepted' and len(posts)==1
    assert bot.send(replace(a,recipient_id="456",conversation_id="bilibili:999:private:456"),{}).state=='failed'
    assert len(posts)==1


def test_deleted_autonomous_source_does_not_reenter_public_context_or_backfill(factory,now):
    e,s,m,_=factory();enable(e,m)
    assert e.autonomy.run(now) and e.dispatch_one(now+600)
    record=s.inspect("conversation_records")[0]
    s.forget_source(record["event_id"])
    s.memory_backfill()
    assert not s.autonomy_recent_public()
    assert all(not r["active"] for r in s.inspect("conversation_records"))


def test_new_memory_revision_during_generation_discards_proposal(factory,now):
    e,s,m,_=factory();enable(e,m)
    original=m.decide
    def decide(op,payload):
        result=original(op,payload);s.set_setting("memory_epoch",1);return result
    m.decide=decide
    assert not e.autonomy.run(now) and not s.inspect("actions")


def test_owner_binding_changed_during_generation_cannot_redirect_proposal(factory,now):
    e,s,m,_=factory();enable(e,m,channel="private")
    original=m.decide
    def decide(op,payload):
        result=original(op,payload);s.bind("owner","bilibili","changed");return result
    m.decide=decide
    assert not e.autonomy.run(now) and not s.inspect("actions")


def test_future_private_plan_does_not_block_an_immediate_reactive_reply(factory,now):
    e,s,m,p=factory();enable(e,m,channel="private")
    assert e.autonomy.run(now)
    event=Event("incoming","bilibili","robot","424242","private/robot/424242","private_message","有个问题",now+1,"private",reply_to="500",sequence=1)
    e.ingest(event)
    a=Action("reactive",s.agent_id,"bilibili","robot",None,"吾辈在听呢。",now+1,now+3600,
             kind="private_reply",conversation_id=event.conversation_id,event_id=event.event_id,
             visibility="private",recipient_id=event.actor_id,reply_to=event.reply_to,source_sequence=1)
    assert s.enqueue_action(a,draft=False,now=now+1)
    assert e.dispatch_one(now+1)
    assert p.sent[0][0].action_id=="reactive"


def test_coordinator_lease_blocks_duplicate_decision_workers(factory,now):
    from murasamebot.adapters.persistence.sqlite import SQLiteStore
    e,s,m,_=factory();enable(e,m)
    competing=SQLiteStore(s.path,s.agent_id)
    assert competing.acquire_lease("autonomy_decision",now,1200)
    assert not e.autonomy.run(now) and not m.calls


def test_autonomy_defaults_and_dashboard_patch_validation(tmp_path):
    from test_architecture_and_config import write_config
    from murasamebot.bootstrap.config import Config
    c=Config(write_config(tmp_path))
    assert not c.behaviour().autonomy.dynamic_enabled and not c.behaviour().autonomy.private_enabled
    c.save_local({"autonomy":{"private_enabled":True,"max_delay_minutes":45}})
    assert Config(c.path).behaviour().autonomy.max_delay_minutes==45
    for patch in ({"recipient_id":"stranger"},{"private_enabled":"yes"},{"max_delay_minutes":-1},
                  {"weekdays":[]},{"windows":[{"start":"25:00","end":"26:00"}]},{"topics":[]}):
        with pytest.raises((ValueError,TypeError)):
            c.save_local({"autonomy":patch})


def test_private_main_switch_prevents_autonomous_generation(factory,now):
    e,s,m,_=factory(private_messages_enabled=False);enable(e,m,channel="private")
    assert not e.autonomy.run(now) and not m.calls
    assert s.get_setting("autonomy_status:private")["state"]=="private_disabled"


def test_model_may_not_schedule_across_a_closed_window(factory,now):
    e,s,m,_=factory();p,_=enable(e,m,windows=(("19:00","20:00"),));p["delay_minutes"]=90
    assert not e.autonomy.run(now) and not s.inspect("actions")
    assert s.get_setting("autonomy_status:dynamic")["state"]=="outside_window"


def test_missing_same_generation_judgement_requires_human_review(factory,now):
    e,s,m,p=factory(model_review=True,review_strategy="inline");proposal,_=enable(e,m)
    del proposal["can_send"]
    assert e.autonomy.run(now)
    assert s.inspect("actions")[0]["state"]=="draft" and not e.dispatch_one(now+600)
    assert not p.sent
