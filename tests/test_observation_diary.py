from dataclasses import replace
from datetime import datetime
from http.server import ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest

from murasamebot.adapters.persistence.sqlite import SQLiteStore
from murasamebot.application.memory import MemoryService, MemorySettings
from murasamebot.application.owner_history import OwnerHistoryService, OwnerHistorySettings
from murasamebot.bootstrap.config import Config
from murasamebot.bootstrap.runtime import prepare_store
from murasamebot.bootstrap.web import handler_for
from murasamebot.domain.models import Event, ModelError
from test_architecture_and_config import write_config
from test_owner_history import record, setup


class DiaryModel:
    def __init__(self):
        self.calls=[]
        self.after=lambda:None
        self.fail=False

    def decide(self,op,payload):
        assert op=="observation_diary"
        self.calls.append(payload)
        self.after()
        if self.fail:raise ModelError("模拟日记服务失败")
        return {"diary":"今日见主人点看了游戏创作的话题，吾辈也生出些好奇。若有机会，想听听主人自己的感想。"}


def journal(factory,now):
    e,s,_,rows,observations=setup(factory)
    observations.sync(now)
    rows.append(record(now+61,title="游戏创作的私有观看线索"))
    observations.sync(now+62)
    model=DiaryModel()
    service=MemoryService(s,model,MemorySettings(),observations=observations,persona="吾辈是丛雨。")
    night=datetime.fromtimestamp(now,ZoneInfo("Asia/Hong_Kong")).replace(hour=23,minute=0,second=0).timestamp()
    return s,rows,observations,model,service,night


def test_observation_diary_uses_new_retained_records_without_faking_dialogue(factory,now):
    s,rows,obs,model,memory,night=journal(factory,now)
    assert not memory.maintain(now+90) and not model.calls
    assert not obs.private_context("owner",night)  # diary is not limited to the recent chat window
    assert memory.maintain(night)
    diary=s.memory_feed("diary")[0]
    assert diary["purpose"]=="owner_observation_diary" and diary["scope"]=="observation:bilibili"
    assert model.calls[0]["persona"]=="吾辈是丛雨。"
    assert "私有观看线索" in str(model.calls[0]["records"])
    assert not {"auth","grant","external_id","binding_version","revision"}&model.calls[0].keys()
    assert s.memory_feed("reflection")==[] and s.memory_feed("memory")==[]
    assert not s.inspect("conversation_records") and not s.inspect("memories")
    assert not s.inspect("tasks") and not s.inspect("actions")
    sources=s.memory_product_source_records(diary["product_id"])
    assert len(sources)==1 and sources[0]["role"]=="observation"
    for visibility in ("private","public"):
        event=Event("probe", "bilibili","robot","424242","new-chat","private_message","聊聊游戏",night,visibility)
        assert memory.context(event,"owner",visibility=="private",night)["memories"]==[]
    assert not memory.maintain(night+1) and len(model.calls)==1
    restarted=SQLiteStore(s.path,s.agent_id)
    assert len(restarted.memory_feed("diary"))==1


@pytest.mark.parametrize("reason",["disabled","diary_off","baseline","budget"])
def test_no_journal_for_disabled_baseline_or_exhausted_budget(factory,now,reason):
    s,rows,obs,model,memory,night=journal(factory,now)
    if reason=="disabled":obs.settings=replace(obs.settings,enabled=False)
    elif reason=="diary_off":obs.settings=replace(obs.settings,diary_enabled=False)
    elif reason=="budget":memory.settings=replace(memory.settings,daily_call_limit=0)
    elif reason=="baseline":
        s.owner_history_reset("owner",now+70)
        obs.sync(now+71,force=True)
    assert not memory.maintain(night) and not model.calls and not s.memory_feed("diary")


@pytest.mark.parametrize("change",["clear","revoke","auth","expiry","permission"])
def test_observation_change_during_generation_discards_journal(factory,now,change):
    s,rows,obs,model,memory,night=journal(factory,now)
    def modify():
        if change=="clear":s.owner_history_reset("owner",night)
        elif change=="revoke":s.revoke_binding("owner","bilibili")
        elif change=="auth":obs.source.authorization=lambda b:"other-auth"
        elif change=="expiry":s.owner_history_prune("owner",night+8*86400,7)
        else:obs.settings=replace(obs.settings,diary_enabled=False)
    model.after=modify
    assert not memory.maintain(night)
    assert not s.memory_feed("diary")
    assert s.inspect("memory_jobs")[0]["state"]=="stale"


@pytest.mark.parametrize("change",["clear","revoke","expiry","forget"])
def test_observation_diaries_follow_source_retention_and_forgetting(factory,now,change):
    s,rows,obs,model,memory,night=journal(factory,now)
    assert memory.maintain(night)
    pid=s.memory_feed("diary")[0]["product_id"]
    if change=="clear":s.owner_history_reset("owner",night+1)
    elif change=="revoke":s.revoke_binding("owner","bilibili")
    elif change=="expiry":s.owner_history_prune("owner",night+8*86400,7)
    else:
        s.memory_edit(pid,forget=True,now=night+1)
        assert s.owner_history_rows("owner",night+1)
        assert not memory.maintain(night+2) and len(model.calls)==1
    assert not s.memory_feed("diary") and not s.memory_product_source_records(pid)
    assert "游戏" not in s.inspect("memory_products")[0]["text"]


def test_failed_journal_retains_sources_and_obeys_retry_delay(factory,now):
    s,rows,obs,model,memory,night=journal(factory,now)
    model.fail=True
    assert not memory.maintain(night)
    assert s.owner_history_rows("owner",night) and not s.memory_feed("diary")
    assert not memory.maintain(night+1) and len(model.calls)==1
    model.fail=False
    assert memory.maintain(night+memory.settings.retry_minutes*60+1)


@pytest.mark.parametrize("diary_enabled",[False,True])
def test_journal_permission_is_independent_of_private_chat_use(factory,now,diary_enabled):
    s,rows,obs,model,memory,night=journal(factory,now)
    obs.settings=replace(obs.settings,use_for_private=False,diary_enabled=diary_enabled)
    assert memory.maintain(night)==diary_enabled
    assert bool(s.memory_feed("diary"))==diary_enabled
    assert not s.inspect("actions")


def test_observation_diary_shares_daily_maintenance_budget_and_retries_uncovered_sources(factory,now):
    s,rows,obs,model,memory,night=journal(factory,now)
    memory.settings=replace(memory.settings,daily_call_limit=1)
    assert memory.maintain(night)
    rows.append(record(night-5,"43"));obs.sync(night+1,force=True)
    assert not memory.maintain(night+2) and len(model.calls)==1
    assert memory.maintain(night+86400) and len(model.calls)==2


def test_live_configuration_revocation_during_model_call_blocks_journal_commit(tmp_path,now):
    import json
    from murasamebot.bootstrap.runtime import build
    path=write_config(tmp_path);config=Config(path)
    config.save_local({"owner_history":{"enabled":True}})
    s=prepare_store(Config(path));binding=s.binding("owner","bilibili")
    secret=tmp_path/'.secrets/bilibili-owner.json';secret.parent.mkdir(exist_ok=True)
    secret.write_text(json.dumps({"uid":"424242","person_id":"owner","binding_version":binding['version'],"authorization_id":"test-auth","cookies":{"SESSDATA":"test-cookie"}}))
    revision=s.owner_history_state("owner")["revision"]
    assert s.owner_history_commit("owner",binding,revision,"test-auth",[],"",now,7)
    assert s.owner_history_commit("owner",binding,revision,"test-auth",[record(now+61)],"",now+62,7)
    runtime=build(Config(path))
    try:
        model=DiaryModel();runtime.engine.memory_service.model=model
        model.after=lambda:Config(path).save_local({"owner_history":{"diary_enabled":False}})
        night=datetime.fromtimestamp(now,ZoneInfo("Asia/Hong_Kong")).replace(hour=23,minute=0,second=0).timestamp()
        assert not runtime.engine.memory_service.maintain(night)
        assert len(model.calls)==1 and not s.memory_feed("diary")
    finally:runtime.close()


def test_journal_feeds_are_separate_and_authenticated(tmp_path,monkeypatch,now):
    import murasamebot.bootstrap.web as web
    from test_memory import archive
    path=write_config(tmp_path);s=prepare_store(Config(path))
    archive(s,now,count=2)
    records=s.inspect("conversation_records")
    for kind in ("summary","diary","reflection"):
        assert s.memory_commit(records,[{"kind":kind,"text":"这个板块中的中文记录。","sources":[records[0]["record_id"]]}],"fixture-"+kind,0,now)
    watched=[]
    source=SimpleNamespace(authorization=lambda b:"auth",read_history=lambda b,**kw:(watched,""))
    obs=OwnerHistoryService(s,source,OwnerHistorySettings(enabled=True),"owner","bilibili")
    obs.sync(now);watched.append(record(now+1));obs.sync(now+2,force=True)
    assert s.observation_diary_commit(obs.diary_context(now+2,now+3,20),"基于授权观察的小小感想。",0,now+2,7)
    authorized=[False]
    from contextlib import nullcontext
    monkeypatch.setattr(web,"optional_history",lambda *a:nullcontext(SimpleNamespace(diary_authorized=lambda:authorized[0])))
    server=ThreadingHTTPServer(("127.0.0.1",0),handler_for(path,"token"))
    thread=Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}",trust_env=False) as c:
            assert c.get('/api/memory/journals').status_code==401
            feeds=c.get('/api/memory/journals',headers={"Authorization":"Bearer token"}).json()
            assert [p['kind'] for p in feeds['memory']]==['summary']
            assert [p['kind'] for p in feeds['diary']]==['diary']
            assert [p['kind'] for p in feeds['reflection']]==['reflection']
            authorized[0]=True
            feeds=c.get('/api/memory/journals',headers={"Authorization":"Bearer token"}).json()
            assert len(feeds['diary'])==2 and any(p['purpose']=='owner_observation_diary' for p in feeds['diary'])
    finally:
        server.shutdown();server.server_close();thread.join(timeout=3)
