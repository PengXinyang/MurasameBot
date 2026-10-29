from dataclasses import replace
import json
import time
from types import SimpleNamespace
from http.server import ThreadingHTTPServer
from threading import Thread

import httpx
import pytest

from murasamebot.adapters.platforms.bilibili_history import BilibiliOwnerHistory, NAV, HISTORY, QR_START, QR_POLL
from murasamebot.application.owner_history import OwnerHistoryService, OwnerHistorySettings
from murasamebot.bootstrap.config import Config
from murasamebot.bootstrap.owner_history import OwnerLoginManager
from murasamebot.bootstrap.runtime import prepare_store
from murasamebot.bootstrap.web import handler_for
from murasamebot.domain.models import PlatformError
from test_architecture_and_config import write_config
from test_autonomy import enable, queued


def record(now, oid="42", title="游戏角色介绍"):
    return {"content_id":oid,"viewed_at":now,"title":title,"category":"游戏", "author_name":"作者",
            "progress":10,"duration":200,"url":"https://www.bilibili.com/video/av"+oid}


def setup(factory):
    e,store,model,platform = factory()
    rows = []
    source = SimpleNamespace(authorization=lambda binding: "auth" if binding else (_ for _ in ()).throw(PlatformError("missing")),
                             read_history=lambda binding, **kw: (list(rows),""))
    service = OwnerHistoryService(store,source,OwnerHistorySettings(enabled=True,poll_seconds=60),"owner","bilibili")
    e.owner_activity = service
    return e,store,model,rows,service


def test_first_sync_display_only_then_new_records_are_private_context(factory,now):
    e,store,model,rows,service = setup(factory)
    rows.append(record(now-10))
    assert service.sync(now)
    assert len(store.owner_history_rows("owner",now))==1
    assert not service.private_context("owner",now)
    rows.append(record(now+61,"43"))
    assert service.sync(now+62)
    context=service.private_context("owner",now+62)
    assert [r["content_id"] for r in context["owner_activity"]["records"]]==["43"]
    assert not service.private_context("other",now+62)
    assert not store.inspect("conversation_records") and not store.inspect("tasks")


def test_clear_does_not_restore_old_data_and_invalidates_inflight_commit(factory,now):
    e,store,model,rows,service = setup(factory)
    service.sync(now)
    rows.append(record(now+61))
    service.sync(now+62)
    binding=store.binding("owner","bilibili")
    revision=store.owner_history_state("owner")["revision"]
    store.owner_history_reset("owner",now+70)
    assert not store.owner_history_commit("owner",binding,revision,"auth",rows,"",now+71,7)
    assert service.sync(now+72,force=True)
    assert not store.owner_history_rows("owner",now+72)
    assert not service.private_context("owner",now+72)


def test_private_plan_cancelled_by_clear_and_history_never_in_public_payload(factory,now):
    e,store,model,rows,service = setup(factory)
    service.sync(now)
    rows.append(record(now+61,title="私有观看线索"))
    service.sync(now+62)
    enable(e,model,channel="private")
    assert e.autonomy.run(now+63)
    action=queued(store)
    payload=next(p for op,p in model.calls if op=="autonomous_private")
    assert payload["owner_activity"]["records"][0]["title"]=="私有观看线索"
    assert action.owner_activity_version and not service.guard(action,now+63)
    store.owner_history_reset("owner",now+64)
    assert store.inspect("actions")[0]["state"]=="cancelled"
    assert service.guard(action,now+65)
    enable(e,model,channel="dynamic")
    e.autonomy.run(now+66)
    public=next(p for op,p in model.calls if op=="autonomous_post")
    assert "owner_activity" not in public and "私有观看线索" not in json.dumps(public,ensure_ascii=False)


def test_expiry_settings_and_binding_changes_block_context(factory,now):
    e,store,model,rows,service=setup(factory)
    service.sync(now);rows.append(record(now+61));service.sync(now+62)
    assert service.private_context("owner",now+62)
    assert not service.private_context("owner",now+8000)
    service.settings=replace(service.settings,use_for_private=False)
    assert not service.private_context("owner",now+63)
    store.revoke_binding("owner","bilibili")
    assert not service.private_context("owner",now+63)


def test_network_failure_retains_data_does_not_pause_robot(factory,now):
    e,store,model,rows,service=setup(factory)
    rows.append(record(now-10));service.sync(now)
    def failure(*a,**k): raise PlatformError("private server error",code="owner_network_error")
    service.source.read_history=failure
    assert not service.sync(now+61)
    assert store.owner_history_state("owner")["error"]=="owner_network_error"
    assert len(store.owner_history_rows("owner",now+61))==1 and not e.paused()


def history_client(tmp_path,handler):
    client=httpx.Client(transport=httpx.MockTransport(handler))
    return BilibiliOwnerHistory(tmp_path/"owner.json",client=client)


def reply(data,headers=None): return httpx.Response(200,json={"code":0,"data":data},headers=headers)


def binding(): return {"person_id":"owner","external_id":"424242","version":1}


@pytest.mark.parametrize("uid",["424242","999"])
def test_qr_verifies_owner_before_saving_and_does_not_touch_ai(tmp_path,uid):
    ai=tmp_path/"bilibili.json";ai.write_text("original-ai")
    seen=[]
    def handle(request):
        assert request.method=="GET"
        seen.append(str(request.url))
        if str(request.url).startswith(QR_START):return reply({"url":"https://passport.bilibili.com/test","qrcode_key":"qr-key"})
        if str(request.url).startswith(QR_POLL):return reply({"code":0},[("set-cookie","SESSDATA=owner-secret; Domain=.bilibili.com; Path=/"),("set-cookie","DedeUserID="+uid+"; Domain=.bilibili.com; Path=/")])
        assert str(request.url)==NAV and request.headers.get("cookie")=="SESSDATA=owner-secret"
        return reply({"isLogin":True,"mid":int(uid)})
    source=history_client(tmp_path,handle)
    assert source.start_login()[1]=="qr-key"
    if uid=="424242":
        assert source.poll_login("qr-key",binding())=="success"
        saved=json.loads(source.path.read_text())
        assert saved["uid"]==uid and saved["authorization_id"]
        assert "bili_jct" not in saved["cookies"]
    else:
        with pytest.raises(PlatformError,match="不一致"):source.poll_login("qr-key",binding())
        assert not source.path.exists()
    assert ai.read_text()=="original-ai"
    with pytest.raises(PlatformError):source._get("https://api.bilibili.com/x/v2/reply/add")


def test_reader_bounded_pages_and_never_follows_untrusted_urls(tmp_path):
    calls=[]
    def handle(request):
        assert request.method=="GET"
        if str(request.url)==NAV:return reply({"isLogin":True,"mid":424242})
        calls.append(dict(request.url.params))
        n=len(calls)
        return reply({"list":[{"title":"测试","view_at":1000-n,"history":{"oid":i+30*n,"business":"archive"},"uri":"https://evil.invalid","tag_name":"游戏"} for i in range(30)],
                      "cursor":{"max":n,"view_at":1000-n,"business":"archive"}})
    source=history_client(tmp_path,handle)
    source.path.write_text(json.dumps({**binding(),"uid":"424242","binding_version":1,"authorization_id":"auth","cookies":{"SESSDATA":"private"}}))
    rows,warning=source.read_history(binding(),pages=3,since=0)
    assert len(calls)==3 and len(rows)==90 and warning
    assert all(r["url"].startswith("https://www.bilibili.com/video/av") for r in rows)
    changed={**binding(),"version":2}
    with pytest.raises(PlatformError):source.authorization(changed)


def test_dashboard_auth_config_and_qr_never_exposed(tmp_path,monkeypatch):
    path=write_config(tmp_path)
    config=Config(path);store=prepare_store(config)
    class FakeSource:
        def __init__(self):self.path=tmp_path/".secrets/bilibili-owner.json"
        def start_login(self):return "https://passport.bilibili.com/test","secret-qr-key"
        def poll_login(self,key,b):assert b["external_id"]=="424242";return "waiting"
        def close(self):pass
        def authorization(self,b):raise PlatformError("not logged in")
    monkeypatch.setattr("murasamebot.bootstrap.owner_history.history_source",lambda c,*args:FakeSource())
    server=ThreadingHTTPServer(("127.0.0.1",0),handler_for(path,"local-token"))
    Thread(target=server.serve_forever,daemon=True).start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}",trust_env=False) as client:
            auth={"Authorization":"Bearer local-token"}
            for endpoint in ("/api/owner-history","/api/owner-history/qr"):
                assert client.get(endpoint).status_code==401
            assert client.post("/api/owner-history/login/start",json={}).status_code==401
            login=client.post("/api/owner-history/login/start",json={},headers=auth)
            assert login.status_code==200 and "secret-qr-key" not in login.text
            image=client.get("/api/owner-history/qr",headers=auth)
            assert image.status_code==200 and image.content.startswith(b"\x89PNG")
            assert client.post("/api/owner-history/login/poll",json={},headers=auth).json()["state"]=="waiting"
            assert client.get("/api/owner-history",headers=auth).json()["records"]==[]
            assert client.post("/api/config",json={"owner_history":{"pages":4}},headers=auth).status_code==400
            assert client.post("/api/config",json={"owner_history":{"poll_seconds":60}},headers=auth).status_code==200
            assert client.post("/api/owner-history/revoke",json={},headers=auth).status_code==200
            assert not Config(path).data["owner_history"]["enabled"]
    finally:
        server.shutdown();server.server_close()


def test_history_recommendation_uses_only_configured_public_topic_and_review_is_private_safe(factory,now):
    e,store,model,rows,service=setup(factory)
    service.settings=replace(service.settings,recommendations_enabled=True,recommendation_topics=("galgame",))
    service.sync(now)
    rows.append(record(now+61,title="galgame 私有观看细节不应公开"))
    service.sync(now+62)
    assert service.recommend(e,now+63)
    action=queued(store)
    assert action.kind=="recommendation" and action.mentions==("owner",) and action.owner_activity_version
    assert action.memory_epoch==store.get_setting("memory_epoch",0)
    payload=next(p for op,p in model.calls if op=="evaluate_video")
    assert payload["requested_topic"]=="galgame" and "owner_activity" not in payload
    assert "私有观看细节" not in json.dumps(payload,ensure_ascii=False)
    e._review_action(action,now+64)
    review=next(p for op,p in model.calls if op=="review_action")
    assert review["owner_activity"] is None and "私有观看细节" not in json.dumps(review,ensure_ascii=False)
    assert not service.recommend(e,now+64)
    store.owner_history_reset("owner",now+65)
    assert store.inspect("actions")[0]["state"]=="cancelled"


def test_recommendation_is_opt_in_and_requires_topic_match_and_browsing_window(factory,now):
    e,store,model,rows,service=setup(factory)
    service.sync(now);rows.append(record(now+61));service.sync(now+62)
    assert not service.recommend(e,now+63)
    service.settings=replace(service.settings,recommendations_enabled=True,recommendation_topics=("不匹配",))
    assert not service.recommend(e,now+63)
    service.settings=replace(service.settings,recommendation_topics=("游戏",))
    e.settings=replace(e.settings,browsing_enabled=False)
    assert not service.recommend(e,now+63) and not model.calls


def test_clear_during_generation_cannot_enqueue_stale_observation(factory,now):
    e,store,model,rows,service=setup(factory)
    service.sync(now);rows.append(record(now+61));service.sync(now+62)
    enable(e,model,channel="private")
    original=model.decide
    def decide(operation,payload):
        result=original(operation,payload)
        if operation=="autonomous_private":store.owner_history_reset("owner",now+64)
        return result
    model.decide=decide
    assert not e.autonomy.run(now+63) and not store.inspect("actions")


def test_qr_success_enables_sync_expiry_and_binding_changes_are_rejected(tmp_path,monkeypatch):
    config=Config(write_config(tmp_path));store=prepare_store(config)
    class Source:
        def start_login(self):return "https://passport.bilibili.com/test","key"
        def poll_login(self,key,b):return "success"
        def close(self):pass
    monkeypatch.setattr("murasamebot.bootstrap.owner_history.history_source",lambda c,*args:Source())
    manager=OwnerLoginManager()
    manager.start(config,store)
    assert manager.poll(config,store)["state"]=="success"
    assert Config(config.path).data["owner_history"]["enabled"] and manager.pending is None
    manager.start(config,store);manager.pending["expires"]=0
    assert manager.poll(config,store)["state"]=="expired"
    manager.start(config,store);store.revoke_binding("owner","bilibili")
    with pytest.raises(ValueError):manager.poll(config,store)


def test_new_history_keeps_private_opening_valid_until_referenced_observation_expires(factory,now):
    e,store,model,rows,service=setup(factory)
    service.sync(now);rows.append(record(now+61));service.sync(now+62)
    enable(e,model,channel="private");e.autonomy.run(now+63)
    action=queued(store)
    rows.append(record(now+121,"43"));service.sync(now+122)
    assert not service.guard(action,now+122)
    assert service.guard(action,now+8*86400)
    assert not store.owner_history_rows("owner",now+8*86400)


def test_progress_refresh_window_rolloff_and_transient_error_preserve_observed_fact(factory,now):
    e,store,model,rows,service=setup(factory)
    service.sync(now);rows.append(record(now+61));service.sync(now+62)
    enable(e,model,channel="private");e.autonomy.run(now+63)
    action=queued(store)
    rows[0]["progress"]=150
    rows.append(record(now+121,"43",title="后来新增的观看线索"));service.sync(now+122)
    assert not service.guard(action,now+123)
    review=service.review_context(action,now+123)
    assert [r["content_id"] for r in review["records"]]==["42"]
    assert review["records"][0]["progress"]==150
    # Recent-context rollover and collection settings do not revoke the fact.
    service.settings=replace(service.settings,context_limit=1,poll_seconds=600,window_minutes=1)
    assert not service.private_context("owner",now+8000)
    assert not service.guard(action,now+8000)
    state=store.owner_history_state("owner")
    store.owner_history_status("owner",state["revision"],{"state":"error","error":"owner_network_error"})
    assert not service.guard(action,now+8000)
    assert service.guard(action,now+8*86400)


@pytest.mark.parametrize("change",["auth","private_off","disabled","binding","source_deleted","auth_error","legacy"])
def test_observation_grants_and_referenced_sources_still_gate_sending(factory,now,change):
    e,store,model,rows,service=setup(factory)
    service.sync(now);rows.append(record(now+61));service.sync(now+62)
    enable(e,model,channel="private");e.autonomy.run(now+63)
    action=queued(store)
    if change=="auth":service.source.authorization=lambda b:"new-auth"
    elif change=="private_off":service.settings=replace(service.settings,use_for_private=False)
    elif change=="disabled":service.settings=replace(service.settings,enabled=False)
    elif change=="binding":store.revoke_binding("owner","bilibili")
    elif change=="source_deleted":
        with store.connection() as db:db.execute("DELETE FROM owner_history WHERE record_id=?",action.owner_activity_sources)
    elif change=="auth_error":
        state=store.owner_history_state("owner")
        store.owner_history_status("owner",state["revision"],{"state":"error","error":"owner_identity_mismatch"})
    elif change=="legacy":action=replace(action,owner_activity_sources=(),owner_activity_version="old-snapshot")
    assert service.guard(action,now+64)


def test_planned_private_message_dispatches_after_new_history(factory,now):
    e,store,model,rows,service=setup(factory)
    service.sync(now);rows.append(record(now+61));service.sync(now+62)
    enable(e,model,channel="private");e.autonomy.run(now+63)
    action=queued(store)
    rows.append(record(now+121,"43"));service.sync(now+122)
    assert e.dispatch_one(action.not_before)
    assert store.inspect("actions")[0]["state"]=="accepted"


def test_regular_private_reply_uses_history_only_for_verified_owner(factory,now):
    from test_private_messages import chat_engine, private_event
    e,store,model,_,_=chat_engine(factory)
    rows=[]
    source=SimpleNamespace(authorization=lambda binding:"auth",read_history=lambda binding,**kw:(list(rows),""))
    service=OwnerHistoryService(store,source,OwnerHistorySettings(enabled=True),"owner","bilibili")
    e.owner_activity=service
    service.sync(now);rows.append(record(now+61,title="只给主人可见的观看线索"));service.sync(now+62,force=True)
    e.ingest(private_event(now+63));e.process_one_event(now+63)
    payload=next(p for op,p in model.calls if op=="private_chat")
    assert payload["owner_activity"]["records"][0]["title"]=="只给主人可见的观看线索"
    action=queued(store)
    assert action.kind=="private_reply" and not service.guard(action,now+63)
    e.ingest(private_event(now+64,peer="123",key="other"));e.process_one_event(now+64)
    other=[p for op,p in model.calls if op=="private_chat"][-1]
    assert "owner_activity" not in other and "只给主人可见" not in json.dumps(other,ensure_ascii=False)


def test_retention_reduction_prunes_records(factory,now):
    e,store,model,rows,service=setup(factory)
    rows.append(record(now-2*86400));service.sync(now)
    assert store.owner_history_rows("owner",now)
    store.owner_history_prune("owner",now,1)
    assert not store.owner_history_rows("owner",now)


@pytest.mark.parametrize("url",[
    "https://account.bilibili.com/h5/account-h5/auth/scan-web?qrcode_key=test-key",
    "https://passport.bilibili.com/h5-app/passport/login/scan?qrcode_key=test-key",
    "https://account.bilibili.com:443/h5/account-h5/auth/scan-web?qrcode_key=test-key",
])
def test_qr_accepts_current_account_host_and_legacy_passport_host(tmp_path,url):
    def handle(request):
        assert str(request.url)==QR_START and request.method=="GET"
        return reply({"url":url,"qrcode_key":"test-key"})
    source=history_client(tmp_path,handle)
    assert source.start_login()==(url,"test-key")
    assert not source.path.exists()


@pytest.mark.parametrize("url,key",[
    ("http://account.bilibili.com/h5/account-h5/auth/scan-web","key"),
    ("https://account.bilibili.com.evil.invalid/scan","key"),
    ("https://evil.invalid/account.bilibili.com/scan","key"),
    ("https://name:password@account.bilibili.com/scan","key"),
    ("https://@account.bilibili.com/scan","key"),
    ("https://account.bilibili.com:444/scan","key"),
    ("https://account.bilibili.com:bad/scan","key"),
    ("https://account.bilibili.com/scan\n","key"),
    ("https://account.bilibili.com\\@evil.invalid/scan","key"),
    ("https://account.bilibili.com/scan",None),
    ("https://account.bilibili.com/scan",42),
    ("https://account.bilibili.com/scan"," "),
    ({"url":"https://account.bilibili.com/scan"},"key"),
])
def test_qr_rejects_foreign_urls_credentials_ports_and_invalid_keys(tmp_path,url,key):
    source=history_client(tmp_path,lambda request:reply({"url":url,"qrcode_key":key}))
    with pytest.raises(PlatformError) as exc:
        source.start_login()
    assert exc.value.code=="owner_qr_invalid" and not source.path.exists()
