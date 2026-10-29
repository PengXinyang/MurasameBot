from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from murasamebot.adapters.persistence.sqlite import SQLiteStore
from murasamebot.application.social import SocialService, SocialSettings
from murasamebot.domain.models import Action, ContentRef, Document, Event, VisualEvidence
from test_autonomy import enable, queued
from test_private_messages import chat_engine, private_event


def update(**patch):
    return {"attention":"later","source":"message","quote":"别的AI","summary":"主人提到与别的AI聊天，这是主人自己的陈述。",
            "owner_related":True,"feeling":"jealous","intensity":1,"note":"吾辈有一点在意，想轻轻撒个娇。",
            "followup_minutes":15,"followup_channel":"either","resolve_ids":[],**patch}


def record(e, s, now, *, actor="424242", visibility="private", key="social-one", conversation=None, patch=None):
    ev = Event(key,"bilibili","robot",actor,conversation or "chat/"+actor,
               "private_message" if visibility=="private" else "mention","今天去和别的AI聊了几句，秘密口令紫色布丁。",now,visibility)
    s.enqueue_event(ev);s.finish_event(ev.event_id,"done")
    person=s.resolve_person(ev.platform,ev.actor_id)
    captured=e.social.capture({"social_update":update(**(patch or {}))},event=ev,person=person,
                              context={"memory_epoch":s.get_setting("memory_epoch",0)},now=now)
    return ev,captured


def action(e, refs, now, **patch):
    return Action("social-action",e.settings.agent_id,"bilibili","robot",None,"吾辈也想多分一点主人的注意呀。",now,now+3600,
                  social_refs=tuple(refs),social_version=e.settings.social.version(),**patch)


def test_provenance_and_role_state_survive_restart_without_becoming_user_facts(factory,now):
    e,s,_,_=factory();ev,captured=record(e,s,now)
    row=s.social_rows(now)[0]
    assert row["source"]["event_id"]==ev.event_id and row["source"]["certainty"]=="owner_statement"
    assert row["source"]["quote"]=="别的AI" and row["state"]["feeling"]=="jealous"
    assert not s.inspect("memory_products") and not s.inspect("memories")
    restarted=SQLiteStore(s.path,s.agent_id)
    service=SocialService(restarted,owner_person_id="owner")
    assert service.context(event=ev,person="owner",now=now+1)["states"][0]["id"]==captured["id"]


def test_owner_private_feeling_can_be_public_without_private_facts_or_provenance(factory,now):
    e,s,_,_=factory();ev,_=record(e,s,now)
    public=e.social.context(channel="dynamic",now=now+901,due_only=True)
    assert public["states"][0]["projection"]=="public_feeling"
    assert public["states"][0]["feeling"]=="jealous"
    encoded=json.dumps(public,ensure_ascii=False)
    for secret in (ev.text,"别的AI","秘密口令","紫色布丁",ev.conversation_id,ev.event_id,"想轻轻撒个娇"):
        assert secret not in encoded
    assert e.social.context(event=ev,person="owner",now=now)["states"][0]["quote"]=="别的AI"


def test_third_party_private_conversation_is_never_exported_to_owner_or_public(factory,now):
    e,s,_,_=factory();ev,_=record(e,s,now,actor="123")
    assert not e.social.context(channel="dynamic",now=now+1000,due_only=True)["states"]
    owner=private_event(now+1)
    assert not e.social.context(event=owner,person="owner",now=now+1)["states"]
    assert e.social.context(event=ev,person=None,now=now)["states"]


def test_public_report_is_attributed_without_granting_owner_identity(factory,now):
    e,s,_,_=factory();ev,captured=record(e,s,now,actor="123",visibility="public")
    row=s.social_rows(now)[0]
    assert row["source"]["certainty"]=="reported" and row["source"]["person_id"]=="unlinked:bilibili:123"
    assert s.resolve_person(ev.platform,ev.actor_id) is None
    ctx=e.social.context(channel="dynamic",now=now+901,due_only=True)
    assert ctx["states"][0]["certainty"]=="reported" and ctx["states"][0]["projection"]=="public_event"


@pytest.mark.parametrize("patch", [{"quote":"不存在的证据"},{"attention":[]},{"feeling":[]},{"source":{}},
                                   {"followup_channel":[]},{"followup_minutes":True},{"followup_minutes":9999},
                                   {"intensity":0},{"resolve_ids":["fabricated"]},{"source":"visual"}])
def test_bad_optional_model_update_is_ignored_without_creating_state(factory,now,patch):
    e,s,_,_=factory();record(e,s,now,patch=patch)
    assert not s.social_rows(now)
    assert s.inspect("audit")[0]["kind"]=="social_update_invalid"


def test_original_event_retry_does_not_duplicate_or_recreate_forgotten_state(factory,now):
    e,s,_,_=factory();ev,captured=record(e,s,now)
    again=e.social.capture({"social_update":update()},event=ev,person="owner",now=now+1)
    assert again["id"]==captured["id"] and len(s.social_rows(now+1))==1
    s.social_forget(captured["id"])
    assert not e.social.capture({"social_update":update()},event=ev,person="owner",now=now+2)["id"]
    assert not s.social_rows(now+2) and s.inspect("social_events")[0]["active"]==0


def test_state_expiry_and_source_retention_scrub_different_data(factory,now):
    e,s,_,_=factory();_,captured=record(e,s,now)
    assert not s.social_rows(now+24*3600+1)
    row=s.social_rows(now+24*3600+1,include_inactive=True)[0]
    assert row["status"]=="expired" and not row["state"]["note"] and row["source"]["quote"]
    assert not s.social_rows(now+8*86400,include_inactive=True)[0]["source"]["quote"]


def test_clarification_resolves_old_state_and_cancels_its_pending_post(factory,now):
    e,s,m,_=factory();ev,captured=record(e,s,now);proposal,_=enable(e,m)
    proposal.update(social_state_ids=[captured["id"]],topic_class="casual")
    assert e.autonomy.run(now+901)
    pending=queued(s)
    clarification=private_event(now+902,key="clarify",text="之前只是普通聊天，别担心啦。")
    s.enqueue_event(clarification);s.finish_event(clarification.event_id,"done")
    context=e.social.context(event=clarification,person="owner",now=now+902)
    out=e.social.capture({"social_update":update(attention="resolve",resolve_ids=[captured["id"]])},
                         event=clarification,person="owner",social_context=context,now=now+902)
    assert out["resolved"]==[captured["id"]] and not s.social_rows(now+902)
    assert s.inspect("actions")[0]["state"]=="cancelled"
    assert e.social.guard(pending,now+1501)
    assert not e.social.references(context,out) and not e.social.followups({"social_state_ids":[captured["id"]]},context,out)


def test_older_or_unrelated_message_cannot_resolve_newer_owner_state(factory,now):
    e,s,_,_=factory();_,captured=record(e,s,now)
    for offset,actor in ((-10,"424242"),(1,"123")):
        ev=private_event(now+offset,peer=actor,key="clarify-"+actor,text="这件事已经结束了。")
        s.enqueue_event(ev);s.finish_event(ev.event_id,"done")
        raw={"states":[{"id":captured["id"],"version":1}]}
        e.social.capture({"social_update":update(attention="resolve",resolve_ids=[captured["id"]])},event=ev,
                         person=s.resolve_person("bilibili",actor),social_context=raw,now=now+2)
    assert s.social_rows(now+3)[0]["status"]=="active"


@pytest.mark.parametrize("invalidate", ["forget","revoke","end"])
def test_forgetting_binding_revocation_and_manual_end_block_even_manual_resend(factory,now,invalidate):
    e,s,m,_=factory();ev,captured=record(e,s,now);proposal,_=enable(e,m)
    proposal.update(social_state_ids=[captured["id"]],topic_class="casual")
    assert e.autonomy.run(now+901)
    pending=queued(s)
    if invalidate=="forget":s.forget_source(ev.event_id)
    elif invalidate=="revoke":s.revoke_binding("owner","bilibili")
    else:s.social_forget(captured["id"],resolve=True)
    assert s.validate_action(pending,now+1501) or e.social.guard(pending,now+1501)
    with pytest.raises(ValueError):e.queue_manual_send(pending.action_id,now+1501)


def test_public_plan_is_generated_from_projection_and_consumes_followup_only_after_acceptance(factory,now):
    e,s,m,p=factory();ev,captured=record(e,s,now);proposal,_=enable(e,m)
    proposal.update(social_state_ids=[captured["id"]],topic_class="casual")
    assert e.autonomy.run(now+901)
    assert s.social_rows(now+901)[0]["followups"]==0
    request=next(payload for op,payload in m.calls if op=="autonomous_post")
    assert "紫色布丁" not in json.dumps(request,ensure_ascii=False)
    assert queued(s).kind=="autonomous_post" and queued(s).social_refs[0]["projection"]=="public_feeling"
    assert not e.autonomy.run(now+902)  # draft/queued plan is reserved, not repeatedly regenerated
    assert e.dispatch_one(now+1501)
    assert len(p.sent)==1 and s.social_rows(now+1501)[0]["followups"]==1
    assert not e.social.context(channel="dynamic",now=now+1502,due_only=True)["states"]


def test_draft_rejected_and_ambiguous_messages_do_not_count_as_spoken(factory,now):
    from murasamebot.domain.models import Receipt
    e,s,m,p=factory(model_review=True,review_strategy="inline");_,captured=record(e,s,now);proposal,_=enable(e,m)
    proposal.update(social_state_ids=[captured["id"]],topic_class="casual",can_send=False)
    assert e.autonomy.run(now+901) and s.inspect("actions")[0]["state"]=="draft"
    assert s.social_rows(now+901)[0]["followups"]==0 and e.social.context(channel="dynamic",now=now+902,due_only=True)=={"states":[]}
    e.queue_manual_send(queued(s).action_id,now+1501)
    p.receipt=Receipt("unknown",detail="write uncertain")
    assert e.dispatch_one(now+1501)
    assert s.social_rows(now+1501)[0]["followups"]==0
    assert not e.social.context(channel="dynamic",now=now+1502,due_only=True)["states"]


def test_private_feeling_public_switch_and_whole_social_switch_are_rechecked(factory,now):
    e,s,_,_=factory();_,captured=record(e,s,now)
    ctx=e.social.context(channel="dynamic",now=now+901,due_only=True)
    old=action(e,e.social.references(ctx),now+901)
    for patch in ({"private_feelings_public":False},{"enabled":False}):
        service=SocialService(s,replace(e.settings.social,**patch))
        assert not service.context(channel="dynamic",now=now+901,due_only=True)["states"]
        assert service.guard(old,now+902)


def test_new_generation_does_not_restore_newly_deleted_source(factory,now):
    e,s,m,p,access=chat_engine(factory)
    previous=m.decide
    ev=private_event(now,text="今天去和别的AI聊了几句。")
    def decide(op,payload):
        if op=="private_chat":
            s.forget_source(ev.event_id)
            return {"decision":"reply","text":"吾辈有一点在意。","social_update":update()}
        return previous(op,payload)
    m.decide=decide;e.ingest(ev);e.process_one_event(now)
    assert not s.social_rows(now) and not s.inspect("actions")


def test_reactive_private_reply_records_care_in_same_call_and_denies_non_mutual(factory,now):
    e,s,m,p,access=chat_engine(factory)
    original=m.decide
    def decide(op,payload):
        result=original(op,payload)
        if op=="private_chat":result["social_update"]=update(attention="ask")
        return result
    m.decide=decide
    e.ingest(private_event(now,text="今天去和别的AI聊了几句。"));assert e.process_one_event(now)
    assert len(s.social_rows(now))==1 and len(s.inspect("actions"))==1
    assert [op for op,_ in m.calls].count("private_chat")==1
    access.allowed=False
    e.ingest(private_event(now+1,key="denied",text="今天去和别的AI聊了几句。"));e.process_one_event(now+1)
    assert len(s.social_rows(now+1))==1


def test_visual_evidence_can_be_source_but_plan_without_analysis_cannot(factory,now):
    e,s,_,_=factory();ev=Event("visual-source","bilibili","robot","123","public-thread","mention","看这张图",now)
    s.enqueue_event(ev);s.finish_event(ev.event_id,"done")
    doc=Document(ContentRef("bilibili","post","99"),"动态","", "visual",visual=VisualEvidence("complete","截图显示两个账号聊天。",(1,),1))
    result={"social_update":update(source="visual",quote="两个账号聊天",summary="有人发来一张聊天截图，身份尚未核实。")}
    assert e.social.capture(result,event=ev,document=doc,now=now)["id"]
    ev=replace(ev,event_id="visual-failed");s.enqueue_event(ev)
    assert not e.social.capture(result,event=ev,document=replace(doc,visual=replace(doc.visual,indices=())),now=now)["id"]


@pytest.mark.parametrize("comment_id", ["", "parent", "invented"])
def test_source_can_reference_actual_post_or_related_parent_comment(factory,now,comment_id):
    e,s,_,_=factory();ev=Event("content-source","bilibili","robot","123","thread","mention","丛雨你看",now)
    s.enqueue_event(ev)
    doc=Document(ContentRef("bilibili","post","99"),"动态","听说主人找别的AI聊天啦。","text",
                 comments=({"id":"parent","text":"有人说主人给别的AI送了布丁。"},))
    result={"social_update":update(source="content",source_comment_id=comment_id)}
    captured=e.social.capture(result,event=ev,document=doc,now=now)
    if comment_id=="invented":
        assert not captured["id"]
    else:
        assert captured["id"]
        row=s.social_rows(now)[0]
        assert row["source"]["source_comment_id"]==comment_id and row["source"]["content"]["external_id"]=="99"


def test_independent_review_of_public_feeling_does_not_receive_private_source(factory,now):
    e,s,m,_=factory(model_review=True,review_strategy="independent");_,captured=record(e,s,now);proposal,_=enable(e,m)
    proposal.update(social_state_ids=[captured["id"]],topic_class="casual")
    original=m.decide
    def decide(op,payload):
        if op=="review_action":
            assert "紫色布丁" not in json.dumps(payload,ensure_ascii=False)
            assert payload["social_context"]["states"][0]["projection"]=="public_feeling"
            return {"decision":"approve","reason":"仅表达自己的轻微感受"}
        return original(op,payload)
    m.decide=decide
    assert e.autonomy.run(now+901)
    assert e.review_one(now+902)


def test_cross_agent_and_unrelated_public_conversation_are_isolated(factory,now):
    e,s,_,_=factory();ev,captured=record(e,s,now,actor="123",visibility="public",conversation="thread-one")
    other=replace(ev,event_id="other",actor_id="456",conversation_id="thread-two")
    assert not e.social.context(event=other,channel="public",now=now)["states"]
    agent=SQLiteStore(s.path,"other-agent")
    assert not agent.social_rows(now)


def test_max_active_and_notice_mode_do_not_schedule_automatic_followups(factory,now):
    e,s,_,_=factory(social=replace(SocialSettings(),max_active_states=1))
    record(e,s,now,patch={"attention":"notice"})
    record(e,s,now+1,key="second")
    assert len(s.social_rows(now+1))==1
    assert not e.social.context(channel="dynamic",now=now+901,due_only=True)["states"]


def test_social_post_requires_classified_topic_and_exact_state_ids(factory,now):
    e,s,m,_=factory();_,captured=record(e,s,now);proposal,_=enable(e,m)
    proposal.update(topic_class="sensitive",social_state_ids=[captured["id"]])
    assert not e.autonomy.run(now+901) and not s.inspect("actions")
    proposal.update(topic_class="casual",social_state_ids=["not-provided"])
    assert not e.autonomy.run(now+3000) and not s.inspect("actions")


def test_reply_continues_after_owner_clarifies_and_ends_state_in_same_generation(factory,now):
    e,s,m,_,_=chat_engine(factory);ev,captured=record(e,s,now)
    original=m.decide
    def decide(op,payload):
        result=original(op,payload)
        if op=="private_chat":
            assert payload["social_context"]["states"][0]["id"]==captured["id"]
            result.update(social_state_ids=[captured["id"]],social_update=update(attention="resolve",resolve_ids=[captured["id"]]))
        return result
    m.decide=decide
    e.ingest(private_event(now+1,key="explanation",text="只是普通聊天，吾辈不用担心啦。"))
    assert e.process_one_event(now+1)
    assert not s.social_rows(now+1) and len(s.inspect("actions"))==1
    assert not json.loads(s.inspect("actions")[0]["payload"])["social_refs"]


def test_history_state_is_source_backed_and_survives_new_history_but_not_clear(factory,now):
    from test_owner_history import setup, record as watched
    e,s,m,rows,history=setup(factory)
    e.social=SocialService(s,e.settings.social,observations=history)
    history.sync(now)
    rows.append(watched(now+61,"43","游戏创作练习"));history.sync(now+62)
    context=history.private_context("owner",now+62)
    result={"social_update":update(source="owner_observation",quote="游戏创作练习",summary="授权观察记录中出现游戏创作话题。",feeling="curious")}
    captured=e.social.capture(result,person="owner",context=context,now=now+63)
    assert captured["id"]
    source=s.social_rows(now+63)[0]["source"]
    assert source["kind"]=="owner_observation" and not source["event_id"]
    assert not s.inspect("conversation_records")
    rows.append(watched(now+123,"44","角色设计"));history.sync(now+124)
    assert e.social.context(channel="dynamic",now=now+1000,due_only=True)["states"]
    s.owner_history_reset("owner",now+1001)
    assert not s.social_rows(now+1001)
    assert not s.social_rows(now+1001,include_inactive=True)[0]["source"]["quote"]


def test_history_jealousy_requires_separate_switch_and_three_distinct_records(factory,now):
    from test_owner_history import setup, record as watched
    e,s,m,rows,history=setup(factory)
    e.social=SocialService(s,e.settings.social,observations=history)
    history.sync(now);rows.append(watched(now+61,"43","角色介绍"));history.sync(now+62)
    result={"social_update":update(source="owner_observation",quote="角色介绍")}
    assert not e.social.capture(result,person="owner",context=history.private_context("owner",now+63),now=now+63)["id"]
    history.settings=replace(history.settings,playful_jealousy=True)
    assert not e.social.capture(result,person="owner",context=history.private_context("owner",now+64),now=now+64)["id"]
    rows.extend([watched(now+122,"44","角色介绍二"),watched(now+123,"45","角色介绍三")]);history.sync(now+124)
    assert e.social.capture(result,person="owner",context=history.private_context("owner",now+125),now=now+125)["id"]


def test_actor_binding_change_and_memory_correction_invalidate_related_state(factory,now):
    e,s,_,_=factory();ev,captured=record(e,s,now)
    records=s.memory_records("owner","conversation:bilibili:"+ev.conversation_id,"private_dialogue")
    source_id=records[0]["record_id"]
    assert s.memory_commit(records,[{"kind":"fact","text":"旧理解","sources":[source_id]}],"fact",0,now)
    pid=s.inspect("memory_products")[0]["product_id"]
    s.memory_edit(pid,text="这是一条更正。",now=now+1)
    assert not s.social_rows(now+1)


def test_social_source_cannot_be_claimed_without_an_actual_event_or_observation(factory,now):
    e,s,_,_=factory()
    assert not e.social.capture({"social_update":update()},person="owner",now=now)["id"]


def test_public_post_cannot_send_a_private_projection_with_a_forged_route(factory,now):
    e,s,_,_=factory();ev,captured=record(e,s,now)
    ctx=e.social.context(event=ev,person="owner",now=now)
    bad=action(e,e.social.references(ctx),now)
    assert s.validate_action(bad,now)=="social_privacy_denied"


def test_autonomous_followup_limit_and_delivery_cooldown_are_rechecked(factory,now):
    e,s,_,_=factory();_,captured=record(e,s,now)
    ctx=e.social.context(channel="dynamic",now=now+901,due_only=True)
    pending=action(e,e.social.references(ctx),now+901,kind="autonomous_post",social_followups=(captured["id"],))
    with s.connection() as db:
        db.execute("UPDATE role_states SET followups=2 WHERE agent_id=?",(s.agent_id,))
    assert e.social.guard(pending,now+902)
    with s.connection() as db:
        db.execute("UPDATE role_states SET followups=0,next_at=? WHERE agent_id=?",(now+2000,s.agent_id))
    assert e.social.guard(pending,now+902)


def test_social_settings_roundtrip_and_invalid_parameters(tmp_path):
    from test_architecture_and_config import write_config
    from murasamebot.bootstrap.config import Config
    config=Config(write_config(tmp_path));config.save_local({"social":{"private_feelings_public":False,"max_followups":1}})
    assert not Config(config.path).behaviour().social.private_feelings_public
    for patch in ({"enabled":"yes"},{"context_limit":101},{"unknown":True},{"followup_min_minutes":721},{"ttl_hours":1}):
        with pytest.raises(ValueError):config.save_local({"social":patch})


def test_default_image_policy_accepts_actual_player_snapshot_cdn(tmp_path):
    import httpx
    from murasamebot.adapters.media.http_images import HTTPImageReader
    from murasamebot.bootstrap.config import Config
    from murasamebot.domain.models import ImageRef
    from test_architecture_and_config import write_config
    from test_video_frames import sprite
    config=Config(write_config(tmp_path))
    def handle(request):
        assert request.url.host=="bimp.hdslb.com" and "cookie" not in request.headers
        return httpx.Response(200,content=sprite())
    reader=HTTPImageReader(hosts=tuple(config.data["vision"]["allowed_image_hosts"]),
                           client=httpx.Client(transport=httpx.MockTransport(handle)))
    try:
        assert reader.load(ImageRef("https://bimp.hdslb.com/preview.jpg",1,(0,0,16,16)),4096).width==16
    finally:
        reader.close()


def test_social_management_requires_auth_and_scrubs_forgotten_sources(tmp_path):
    import time
    from http.server import ThreadingHTTPServer
    from threading import Thread
    import httpx
    from murasamebot.bootstrap.config import Config
    from murasamebot.bootstrap.runtime import prepare_store
    from murasamebot.bootstrap.web import handler_for
    from test_architecture_and_config import write_config
    path=write_config(tmp_path);s=prepare_store(Config(path));now=time.time()
    ev=Event("web-social","bilibili","robot","424242","web-private","private_message","去找别的AI聊天啦。",now,"private")
    s.enqueue_event(ev);s.finish_event(ev.event_id,"done")
    service=SocialService(s);captured=service.capture({"social_update":update()},event=ev,person="owner",now=now)
    server=ThreadingHTTPServer(("127.0.0.1",0),handler_for(path,"test-token"));thread=Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}",trust_env=False) as client:
            assert client.get('/api/social').status_code==401
            headers={"Authorization":"Bearer test-token"}
            data=client.get('/api/social',headers=headers).json()
            assert data['records'][0]['source']['quote']=='别的AI'
            assert client.post('/api/social/manage',json={'id':captured['id'],'operation':'resolve'},headers={**headers,'Origin':'https://evil.invalid'}).status_code==401
            assert client.post('/api/social/manage',json={'id':captured['id'],'operation':'resolve'},headers=headers).status_code==200
            assert client.get('/api/social',headers=headers).json()['records'][0]['status']=='resolved'
            assert client.post('/api/social/manage',json={'id':captured['id'],'operation':'forget'},headers=headers).status_code==200
            assert not client.get('/api/social',headers=headers).json()['records'][0]['source']['quote']
    finally:
        server.shutdown();server.server_close();thread.join()
