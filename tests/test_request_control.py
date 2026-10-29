from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import json
from types import SimpleNamespace

import httpx
import pytest

from murasamebot.adapters.request_monitor import RequestMonitor, MonitoredTransport
from murasamebot.application.request_policy import RequestSettings
from murasamebot.adapters.platforms.bilibili import BilibiliAdapter
from murasamebot.bootstrap.config import Config
from murasamebot.bootstrap.runtime import Runtime
from murasamebot.domain.models import Action, PlatformError, PrivateInbox, Receipt
from test_bilibili_contracts import credentials, nav_data
from test_architecture_and_config import write_config
from test_autonomy import enable, queued


class Clock:
    def __init__(self,now):self.now=now;self.waits=[]
    def __call__(self):return self.now
    def sleep(self,seconds):self.waits.append(seconds);self.now+=seconds


def monitor(store,clock,**overrides):
    return RequestMonitor(store,replace(RequestSettings(),**overrides),clock=clock,sleep=clock.sleep)


def test_shared_slots_are_atomic_and_bounded(factory,now):
    _,store,_,_=factory()
    with ThreadPoolExecutor(max_workers=8) as pool:
        slots=list(pool.map(lambda _:store.request_slot(now,1,5),range(8)))
    assert sorted(x for x in slots if x is not None)==[now+i for i in range(6)]
    assert slots.count(None)==2


def test_read_spacing_is_shared_between_clients_and_writes_are_not_delayed(factory,now):
    _,store,_,_=factory();clock=Clock(now)
    a,b=monitor(store,clock),monitor(store,clock)
    a.before(httpx.Request("GET","https://api.bilibili.com/x/web-interface/nav"))
    b.before(httpx.Request("GET","https://api.bilibili.com/x/relation"))
    b.before(httpx.Request("POST","https://api.bilibili.com/x/v2/reply/add"))
    assert clock.waits==[1]


def test_endpoint_backoff_does_not_disable_other_channels_and_success_resets(factory,now):
    _,store,_,_=factory();clock=Clock(now);m=monitor(store,clock)
    responses=[-509,-509,0];calls=[]
    def handle(request):
        calls.append(request.url.path)
        code=responses.pop(0) if request.url.path.endswith("get_sessions") else 0
        return httpx.Response(200,json={"code":code,"data":{}})
    client=httpx.Client(transport=MonitoredTransport(httpx.MockTransport(handle),m))
    url="https://api.vc.bilibili.com/session_svr/v1/session_svr/get_sessions"
    client.get(url)
    assert store.request_cooldown("private_inbox")["until"]==now+60
    with pytest.raises(PlatformError):client.get(url)
    client.get("https://api.bilibili.com/x/msgfeed/at")
    assert len(calls)==2 and not store.get_setting("platform_paused:bilibili",False)
    clock.now=now+61;client.get(url)
    assert store.request_cooldown("private_inbox")["until"]==now+181
    clock.now=now+182;client.get(url)
    assert store.request_cooldown("private_inbox")=={}


def test_actual_logs_never_include_query_cookie_body_or_response_message(factory,now):
    _,store,_,_=factory();clock=Clock(now);m=monitor(store,clock)
    client=httpx.Client(transport=MonitoredTransport(httpx.MockTransport(lambda r:httpx.Response(200,json={"code":0,"data":{"text":"response-secret"}})),m))
    client.post("https://api.vc.bilibili.com/web_im/v1/web_im/send_msg?csrf=query-secret",
                headers={"Cookie":"SESSDATA=cookie-secret"},content="private-message-secret")
    state=store.request_status(now,m.settings)
    raw=json.dumps(state)
    assert state["last_minute"]==1 and state["recent"][0]["api_code"]=="0"
    for secret in ("query-secret","cookie-secret","private-message-secret","response-secret","csrf"):
        assert secret not in raw


def test_get_429_honors_retry_after_and_network_error_has_only_type(factory,now):
    _,store,_,_=factory();clock=Clock(now);m=monitor(store,clock)
    c=httpx.Client(transport=MonitoredTransport(httpx.MockTransport(lambda r:httpx.Response(429,headers={"Retry-After":"120"})),m))
    c.get("https://api.bilibili.com/x/msgfeed/at")
    assert store.request_cooldown("notifications")["until"]==now+120
    def fail(r):raise httpx.ConnectError("sensitive-url-secret")
    c=httpx.Client(transport=MonitoredTransport(httpx.MockTransport(fail),m))
    with pytest.raises(httpx.ConnectError):c.get("https://api.bilibili.com/x/relation")
    recent=store.request_status(clock(),m.settings)["recent"]
    assert recent[0]["error"]=="ConnectError" and "sensitive-url-secret" not in json.dumps(recent)


def auto_action(store,now,*,identifier="plan",due=120,draft=False):
    a=Action(identifier,store.agent_id,"bilibili","robot",None,"吾辈来分享日常。",now,now+3600,
             kind="autonomous_post",not_before=now+due,autonomy_version="v")
    assert store.enqueue_action(a,draft=draft,now=now)
    return a


def test_quiet_only_for_queued_plans_is_bounded_and_essential_reads_still_check_cooldown(factory,now):
    _,store,_,_=factory();clock=Clock(now);m=monitor(store,clock)
    auto_action(store,now,draft=True)
    clock.now=now+100
    assert m.allowed("private_inbox")
    auto_action(store,now,identifier="queued")
    clock.now=now+60
    assert not m.allowed("private_inbox") and m.allowed("relations")
    with m.essential_reads():assert m.allowed("private_inbox")
    clock.now=now+136
    assert m.allowed("private_inbox")  # overdue plan cannot stop polling forever


def test_read_deferred_keeps_action_queued_without_sliding_planned_time(factory,now):
    e,store,model,p=factory();enable(e,model)
    e.autonomy.run(now);action=queued(store)
    def fail(*a,**kw):raise PlatformError("读取限流",code="-509",retry_at=now+700)
    p.send=fail
    assert e.dispatch_one(now+600)
    saved=queued(store)
    assert saved.not_before==action.not_before and saved.retry_at==now+700
    assert store.inspect("actions")[0]["state"]=="queued"
    assert not e.dispatch_one(now+650)


def test_private_cadence_is_independent_and_quiet_preserves_checkpoint(factory,now,tmp_path):
    e,store,model,_=factory();config=Config(write_config(tmp_path));clock=Clock(now);m=monitor(store,clock)
    calls=[]
    p=SimpleNamespace(account_id="robot",private_messages=lambda *a,**k:(calls.append(1) or PrivateInbox((),{})))
    rt=Runtime(config,store,p,model,e,network_monitor=m)
    rt.poll_private(now);rt.poll_private(now+15);rt.poll_private(now+30)
    assert len(calls)==2
    old=store.get_setting("private_inbox:robot")
    auto_action(store,now,due=90)
    rt.poll_private(now+60)
    assert len(calls)==2 and store.get_setting("private_inbox:robot")==old


@pytest.mark.parametrize("new_message",[False,True])
def test_scheduled_private_preflight_during_quiet_cancels_new_incoming(tmp_path,factory,now,new_message):
    _,store,_,_=factory();clock=Clock(now+120);m=monitor(store,clock)
    auto_action(store,now,due=120)
    writes=[]
    def handle(request):
        if request.url.path.endswith("nav"):return httpx.Response(200,json={"code":0,"data":nav_data()})
        if request.url.path.endswith("relation"):return httpx.Response(200,json={"code":0,"data":{"mid":123,"attribute":6}})
        if request.url.path.endswith("fetch_session_msgs"):
            rows=[{"sender_uid":123,"receiver_id":999,"receiver_type":1,"timestamp":now+61,"msg_type":2}] if new_message else []
            return httpx.Response(200,json={"code":0,"data":{"messages":rows}})
        writes.append(request);return httpx.Response(200,json={"code":0,"data":{"msg_key":"777"}})
    p=BilibiliAdapter(credentials(tmp_path),proactive_recipient_id="123",monitor=m,
        client=httpx.Client(transport=MonitoredTransport(httpx.MockTransport(handle),m)))
    a=Action("private",store.agent_id,"bilibili","999",None,"主人今日可好？",now,now+1000,kind="proactive_private",
             visibility="private",recipient_id="123",conversation_id="bilibili:999:private:123")
    result=p.send(a,{})
    assert (result.state=="accepted")== (not new_message)
    assert len(writes)==(0 if new_message else 1)


def test_read_limits_do_not_pause_but_auth_and_write_limits_still_pause(tmp_path):
    code=[-509]
    p=BilibiliAdapter(credentials(tmp_path),client=httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={"code":code[0]}))))
    with pytest.raises(PlatformError) as exc:p._request("/x/relation")
    assert not exc.value.pause
    code[0]=-101
    with pytest.raises(PlatformError) as exc:p._request("/x/relation")
    assert exc.value.pause
    code[0]=-509
    with pytest.raises(PlatformError) as exc:p._request("/x/v2/reply/add",data={"message":"test"})
    assert exc.value.pause


def test_owner_identity_backoff_does_not_block_ai_identity(factory,now):
    _,store,_,_=factory();clock=Clock(now)
    owner=RequestMonitor(store,RequestSettings(),"owner",clock=clock,sleep=clock.sleep)
    c=httpx.Client(transport=MonitoredTransport(httpx.MockTransport(lambda r:httpx.Response(200,json={"code":-509})),owner))
    c.get("https://api.bilibili.com/x/web-interface/nav")
    assert not owner.allowed("identity") and monitor(store,clock).allowed("identity")


def test_success_already_inflight_cannot_clear_new_cooldown(factory,now):
    _,store,_,_=factory();clock=Clock(now);m=monitor(store,clock)
    request=httpx.Request("GET","https://api.bilibili.com/x/msgfeed/reply")
    clock.now+=2
    m.record(request,httpx.Response(200,json={"code":-509}),now)
    m.record(request,httpx.Response(200,json={"code":0}),now+1)
    assert not m.allowed("notifications")


def test_duplicate_poll_claims_share_interval(factory,now):
    _,store,_,_=factory()
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims=list(pool.map(lambda _:store.claim_poll_interval("private_inbox",now,30),range(4)))
    assert sum(claims)==1
    assert store.claim_poll_interval("notifications",now,15)


def test_quiet_preflight_cannot_bypass_real_rate_cooldown(factory,now):
    _,store,_,_=factory();clock=Clock(now);m=monitor(store,clock)
    request=httpx.Request("GET","https://api.vc.bilibili.com/svr_sync/v1/svr_sync/fetch_session_msgs")
    m.record(request,httpx.Response(200,json={"code":-509}),now)
    with m.essential_reads():
        assert not m.allowed("private_inbox")
        with pytest.raises(PlatformError):m.before(request)


def test_account_cache_only_applies_to_reads_and_changes_with_credentials(tmp_path,factory,now):
    _,store,_,_=factory();clock=Clock(now);m=monitor(store,clock)
    calls=[]
    def handle(request):calls.append(request.url.path);return httpx.Response(200,json={"code":0,"data":nav_data()})
    first=BilibiliAdapter(credentials(tmp_path),monitor=m,client=httpx.Client(transport=httpx.MockTransport(handle)))
    first.read_account()
    second=BilibiliAdapter(first.credentials_path,monitor=m,client=httpx.Client(transport=httpx.MockTransport(handle)))
    second.read_account();assert len(calls)==1
    second.account();assert len(calls)==2  # write path explicitly revalidates
    second.credentials["SESSDATA"]="changed-session"
    second.read_account();assert len(calls)==3
