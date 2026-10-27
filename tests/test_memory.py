from dataclasses import replace
from datetime import datetime
import json
import sqlite3

import pytest

from murasamebot.adapters.persistence.sqlite import SCHEMA, SQLiteStore
from murasamebot.application.memory import MemoryService, MemorySettings
from murasamebot.domain.models import Action, Event, ModelError, Receipt


def archive(store, now, count=1, *, person="peer", platform="wechat", visibility="private", text="今天聊了普通的日常", conversation="room"):
    events = []
    for i in range(count):
        event = Event(f"{person}-{platform}-{conversation}-{now}-{i}",platform,"robot",person,conversation,"chat",text,now+i,visibility)
        store.enqueue_event(event)
        store.finish_event(event.event_id,"done")
        events.append(event)
    return events


class Summarizer:
    def __init__(self):
        self.calls = []
        self.fail = False
        self.after = lambda: None
        self.supersedes = []

    def decide(self, op, payload):
        self.calls.append((op,payload))
        self.after()
        if self.fail:
            raise ModelError("模拟失败")
        row = next(r for r in payload["records"] if r["role"]=="user")
        return {"summary":"本段对话中用户聊了日常，相关细节仍可查阅原文。",
                "facts":[{"text":"用户提到日常话题。","sources":[row["record_id"]],"quote":row["text"],"supersedes":self.supersedes}],
                "reflection":"今天认真接住了对方的话题。"}


@pytest.fixture
def memory(tmp_path):
    store = SQLiteStore(tmp_path/"memory.db","agent")
    model = Summarizer()
    settings = MemorySettings(recent_messages=2,summary_messages=3,batch_messages=8)
    return store,model,MemoryService(store,model,settings)


def test_v1_migration_has_restorable_backup_and_no_replay(tmp_path):
    path = tmp_path/"legacy.db"
    event = Event("old","wechat","robot","peer","room","chat","以前说过摄影",10,"private")
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
        db.execute("INSERT INTO events(agent_id,event_id,payload,state) VALUES(?,?,?,'done')", ("a","old",json.dumps(event.to_dict())))
    store = SQLiteStore(path,"a")
    assert path.with_name("legacy.db.v1-backup").exists()
    with store.connection() as db:
        assert db.execute("SELECT version FROM schema_version").fetchone()[0]==2
    store.memory_backfill()
    store.memory_backfill()
    assert store.memory_status()["conversation_records"]==1
    assert store.claim_event(100) is None
    assert store.inspect("actions")==[]


def test_old_chinese_interest_survives_1000_messages_and_is_scoped(memory, now):
    store,model,service = memory
    archive(store,now,text="我喜欢摄影入门与构图教程")
    archive(store,now+1,count=1000)
    archive(store,now+3,person="other",text="不能泄露的另一人的私事")
    event = Event("question","wechat","robot","peer","room","chat","还记得我喜欢什么摄影视频吗？",now+2000,"private")
    context = service.context(event,None,False,now+2000)
    assert any("摄影入门" in m["text"] for m in context["memories"])
    assert "另一人的私事" not in str(context)
    assert len(context["history"])==2
    assert not model.calls
    assert store.memory_records("unlinked:wechat:peer","conversation:wechat:room","public_dialogue",query="摄影")==[]
    assert store.memory_records("unlinked:wechat:peer","conversation:qq:room","private_dialogue",query="摄影")==[]
    assert SQLiteStore(store.path,"other-agent").memory_status()["conversation_records"]==0


def test_two_character_search_and_optional_fts_are_equivalent(memory, now):
    store,_,_ = memory
    archive(store,now,text="摄影入门与构图教程")
    for query in ("摄影","摄影入门","构图",'" OR *'):
        result = store.memory_records("unlinked:wechat:peer","conversation:wechat:room","private_dialogue",query=query)
        assert bool(result)==(query!='" OR *')


def test_only_confirmed_outgoing_enters_archive(memory, now):
    store,_,_ = memory
    event = archive(store,now)[0]
    for i,state in enumerate(("draft","failed","unknown","accepted")):
        action = Action(str(i),"agent","wechat","robot",None,"这是一条回复",now,now+1000,event_id=event.event_id,conversation_id="room",visibility="private")
        store.enqueue_action(action,draft=True,now=now)
        if state!="draft":
            with store.connection() as db:
                db.execute("UPDATE actions SET state='sending' WHERE agent_id=? AND action_id=?", ("agent",str(i)))
            store.finish_action(str(i),Receipt(state,"receipt"),now+1)
    rows = store.inspect("conversation_records")
    assert sum(r["role"]=="agent" for r in rows)==1
    store.memory_backfill()
    assert store.memory_status()["conversation_records"]==2


def test_incremental_summary_keeps_recent_messages_and_is_idempotent(memory, now):
    store,model,service = memory
    archive(store,now,count=5)
    assert service.maintain(now+6)
    assert len(model.calls)==1
    assert len(model.calls[0][1]["records"])==3
    products = store.inspect("memory_products")
    assert {p["kind"] for p in products}=={"summary","fact"}
    assert not service.maintain(now+7)
    assert len(model.calls)==1
    assert store.memory_status()["conversation_records"]==5


def test_small_quiet_conversation_cannot_starve_large_conversation(memory, now):
    store,model,service = memory
    archive(store,now,count=3,conversation="quiet")
    archive(store,now+100,count=5,conversation="active")
    assert service.maintain(now+200)
    assert store.inspect("memory_products")[0]["scope"]=="conversation:wechat:active"


def test_pressure_triggers_without_message_threshold(memory, now):
    store,model,service = memory
    service.settings = replace(service.settings,context_characters=1000)
    archive(store,now,count=3,text="今天聊起了从前的一些日常事情。"*100)
    assert service.maintain(now+4)
    assert len(model.calls[0][1]["records"])==1


def test_pressure_can_compress_long_messages_before_default_recent_window_is_full(memory, now):
    store,_,service=memory
    service.settings=replace(service.settings,recent_messages=20,context_characters=1000)
    archive(store,now,count=3,text="需要保留的长篇聊天内容。"*100)
    assert service.maintain(now+4)


def test_failure_keeps_raw_and_coverage_retry_and_budget(memory, now):
    store,model,service = memory
    archive(store,now,count=5)
    model.fail = True
    assert not service.maintain(now+6)
    assert store.memory_status()["pending_summary"]==5
    assert store.inspect("memory_jobs")[0]["state"]=="failed"
    assert not service.maintain(now+7)
    assert len(model.calls)==1
    model.fail = False
    assert service.maintain(now+700)
    assert len(model.calls)==2
    assert store.inspect("memory_daily")[0]["calls"]==2


def test_maintenance_daily_budget_is_hard_cap(memory, now):
    store,model,service = memory
    service.settings = replace(service.settings,daily_call_limit=1)
    archive(store,now,count=5)
    assert service.maintain(now+6)
    archive(store,now+10,count=5)
    assert not service.maintain(now+16)
    assert len(model.calls)==1
    assert store.get_setting("memory_health")["state"]=="budget_exhausted"


def test_forgetting_cascades_and_restart_backfill_never_resurrects(memory, now):
    store,model,service = memory
    events = archive(store,now,count=5,text="关于摄影的日常")
    assert service.maintain(now+6)
    store.forget_source(events[0].event_id)
    assert all(not p["active"] for p in store.inspect("memory_products"))
    restarted = SQLiteStore(store.path,"agent")
    restarted.memory_backfill()
    assert restarted.event(events[0].event_id) is None
    assert restarted.memory_status()["conversation_records"]==4
    assert "[已删除]" in str(restarted.inspect("conversation_records"))
    if restarted.memory_status()["fts5_trigram"]:
        with restarted.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM memory_fts WHERE memory_fts MATCH '摄影的'").fetchone()[0]==4


def test_delete_during_model_call_discards_summary(memory, now):
    store,model,service = memory
    events = archive(store,now,count=5)
    model.after = lambda: store.forget_source(events[0].event_id)
    assert not service.maintain(now+6)
    assert store.inspect("memory_products")==[]
    assert store.inspect("memory_jobs")[0]["state"]=="stale"


def test_revoke_binding_removes_derived_and_legacy_memories(memory, now):
    store,model,service = memory
    store.bind("owner","wechat","peer")
    events = archive(store,now,count=5)
    store.add_memory("owner","person","owner_video_recommendation","摄影",events[0].event_id,now)
    assert service.maintain(now+6)
    store.revoke_binding("owner","wechat")
    assert store.memory_status()["conversation_records"]==0
    assert all(not p["active"] for p in store.inspect("memory_products"))
    assert not store.memory("owner","person","owner_video_recommendation",now)
    SQLiteStore(store.path,"agent").memory_backfill()
    assert store.memory_status()["conversation_records"]==0


def test_daily_review_is_scoped_deduplicated_and_does_not_change_persona(memory, now):
    store,model,service = memory
    archive(store,now,count=2)
    night = datetime.fromisoformat("2026-10-26T23:00:00+08:00").timestamp()
    assert service.maintain(night)
    assert model.calls[0][1]["mode"]=="diary"
    assert {p["kind"] for p in store.inspect("memory_products")}=={"diary","reflection"}
    assert not service.maintain(night+10)
    assert store.inspect("tasks")==[] and store.inspect("actions")==[]


def test_correction_has_version_sources_pin_and_precedence(memory, now):
    store,model,service = memory
    archive(store,now,text="我以前喜欢摄影")
    archive(store,now+1,count=4)
    assert service.maintain(now+6)
    fact = next(p for p in store.inspect("memory_products") if p["kind"]=="fact")
    corrected = store.memory_edit(fact["product_id"],text="用户现在喜欢料理，已更正旧偏好。",now=now+7)
    store.memory_edit(corrected,pinned=True)
    event = Event("question","wechat","robot","peer","room","chat","我喜欢什么？",now+10,"private")
    ctx = service.context(event,None,False,now+10)
    assert ctx["memories"][0]["product_id"]==corrected
    assert "我以前喜欢摄影" not in str(ctx)
    assert next(p for p in store.inspect("memory_products") if p["product_id"]==corrected)["supersedes"]==fact["product_id"]
    store.memory_edit(corrected,forget=True)
    assert all(not p["active"] for p in store.inspect("memory_products"))


def test_invalid_quote_fails_without_partial_product(memory, now):
    store,model,service = memory
    archive(store,now,count=5)
    original = model.decide
    def decide(op,payload):
        result = original(op,payload)
        result["facts"][0]["quote"]="不存在于原文的引文"
        return result
    model.decide = decide
    assert not service.maintain(now+6)
    assert store.inspect("memory_products")==[]


def test_sources_cross_scope_are_rejected_atomically(memory, now):
    store,_,_ = memory
    archive(store,now,count=2)
    archive(store,now,person="other")
    rows = store.inspect("conversation_records")
    assert not store.memory_commit([rows[0],rows[1]],[{"kind":"summary","text":"跨用户内容","sources":[rows[0]["record_id"]]}],"summary",0,now+10)
    assert not store.inspect("memory_products")


def test_rebuild_preserves_corrections_and_tombstones(memory, now):
    store,_,service = memory
    events = archive(store,now,count=5)
    assert service.maintain(now+6)
    fact = next(p for p in store.inspect("memory_products") if p["kind"]=="fact")
    pid = store.memory_edit(fact["product_id"],text="更正后的中文事实。",now=now+7)
    store.forget_source(events[2].event_id)
    store.memory_rebuild("unlinked:wechat:peer","conversation:wechat:room","private_dialogue")
    assert next(p for p in store.inspect("memory_products") if p["product_id"]==pid)["active"]
    assert store.event(events[2].event_id) is None


def test_deleted_context_cannot_dispatch(factory, now):
    engine,store,model,_ = factory()
    event = Event("mention","bilibili","robot","peer","thread","mention","聊聊摄影",now)
    engine.ingest(event)
    engine.process_one_event(now)
    action = Action.from_dict(json.loads(store.inspect("actions")[0]["payload"]))
    assert action.memory_epoch==0
    store.forget_source(event.event_id)
    assert store.validate_action(action,now+1)=="memory_sources_invalidated"
    assert not engine.dispatch_one(now+1)


def test_context_budget_keeps_relevant_old_original_ahead_of_unrelated_summaries(memory, now):
    store,_,service = memory
    old = archive(store,now,text="我想了解摄影入门课程")[0]
    archive(store,now+1,count=4)
    for i in range(6):
        rows = store.memory_records("unlinked:wechat:peer","conversation:wechat:room","private_dialogue",limit=5)
        # Different maintenance kinds simulate many independently stored segments.
        store.memory_commit(rows,[{"kind":"summary","text":"无关日常"*500,"sources":[rows[0]["record_id"]]}],f"fixture-{i}",0,now+i)
    event = replace(old,event_id="ask",text="还记得摄影课程吗",occurred_at=now+20)
    service.settings = replace(service.settings,context_characters=1000)
    context = service.context(event,None,False,now+20)
    assert any("摄影入门" in m["text"] for m in context["memories"])
    metrics=next(iter(store.memory_contexts()))
    assert metrics["characters"]<=1000
    assert metrics["omitted_entries"]>0


def test_sqlite_without_fts_still_recalls_chinese_short_words(memory, now):
    store,_,_ = memory
    archive(store,now,text="摄影入门课程")
    if store.memory_status()["fts5_trigram"]:
        with store.connection() as db:
            for trigger in ("memory_fts_insert","memory_fts_delete","memory_fts_update"):
                db.execute(f"DROP TRIGGER {trigger}")
            db.execute("DROP TABLE memory_fts")
    assert store.memory_records("unlinked:wechat:peer","conversation:wechat:room","private_dialogue",query="摄影")


def test_model_adapter_preserves_all_40_summary_records_and_shares_total_budget(memory, now):
    import httpx
    from murasamebot.adapters.models.chat_completions import ChatCompletionsModel
    store,_,service = memory
    archive(store,now,count=42)
    requests=[]
    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200,json={"choices":[{"finish_reason":"stop","message":{"content":json.dumps({"summary":"用户谈论了日常话题。","facts":[],"reflection":""})}}],"usage":{"prompt_tokens":100,"completion_tokens":20}})
    gateway=ChatCompletionsModel(base_url="https://model.invalid",name="fake",api_key="",store=store,
        timezone="Asia/Hong_Kong",daily_limit=100,client=httpx.Client(transport=httpx.MockTransport(respond)))
    service.model=gateway
    service.settings=replace(service.settings,summary_messages=40,batch_messages=50)
    try:
        assert service.maintain(now+45)
    finally:
        gateway.close()
    assert len(json.loads(requests[0]["messages"][1]["content"])["records"])==40
    assert "max_tokens" not in requests[0]
    assert store.inspect("model_daily")[0]["calls"]==1
    assert store.inspect("memory_daily")[0]["calls"]==1


def test_newer_explicit_fact_can_supersede_old_fact_with_source_chain(memory, now):
    store,model,service=memory
    archive(store,now,count=5)
    assert service.maintain(now+6)
    old=next(p for p in store.inspect("memory_products") if p["kind"]=="fact")
    model.supersedes=[old["product_id"]]
    archive(store,now+10,count=5,text="我现在喜欢料理")
    assert service.maintain(now+20)
    active=[p for p in store.inspect("memory_products") if p["kind"]=="fact" and p["active"]]
    assert len(active)==1 and active[0]["supersedes"]==old["product_id"]
    assert len(store.memory_product_source_records(active[0]["product_id"]))==2
    assert store.get_setting("memory_epoch")==1


def test_concurrent_maintenance_and_restart_recovery(memory, now):
    store,model,service=memory
    archive(store,now,count=5)
    other=SQLiteStore(store.path,"agent")
    assert other.acquire_lease("memory_maintenance",now,100)
    assert not service.maintain(now+1) and not model.calls
    other.release_lease("memory_maintenance")
    store.memory_job("interrupted","summary","running",now-1000)
    assert service.maintain(now+6)
    assert next(j for j in store.inspect("memory_jobs") if j["job_id"]=="interrupted")["state"]=="interrupted"


def test_legacy_owner_preference_can_be_recalled_outside_recent_limit(memory, now):
    store,_,service=memory
    store.bind("owner","wechat","peer")
    store.add_memory("owner","person","owner_video_recommendation","摄影入门","old",now-1000)
    for i in range(40):
        store.add_memory("owner","person","owner_video_recommendation","普通的新兴趣",str(i),now+i)
    event=Event("question","wechat","robot","peer","room","chat","摄影相关的偏好还记得吗",now+100,"private")
    assert any("摄影入门" in m["text"] for m in service.context(event,"owner",True,now+100)["memories"])
    public=replace(event,visibility="public")
    assert "摄影入门" not in str(service.context(public,"owner",True,now+100))


def test_private_relation_denial_does_not_create_dialogue_memory(factory, now):
    engine,store,model,_=factory()
    event=Event("denied","bilibili","robot","peer","private-room","private_message","私信文字",now,"private")
    engine.ingest(event)
    engine.process_one_event(now)
    assert not model.calls
    assert store.inspect("events")[0]["state"]=="denied"
    store.memory_backfill()
    assert store.memory_status()["conversation_records"]==0
