"""Owner-only QQ initiative through shared scheduling and outbox, no live IO."""
from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace

import httpx
import pytest

from murasamebot.application.autonomy import AutonomySettings, AutonomyTarget
from murasamebot.adapters.persistence.chat_batches import ChatBatches
from murasamebot.adapters.platforms.qq import QQAdapter
from murasamebot.bootstrap.platforms.qq import DEFAULTS, QQModule
from murasamebot.domain.models import Action, Event
from test_platform_console import console


def setup(factory, **policy):
    e,s,m,p = factory(model_review=True,review_strategy='inline')
    s.bind('owner','qq','123')
    writes, friends = [], [True]
    def handle(request):
        if request.url.path == '/get_login_info': data = {'user_id':456}
        elif request.url.path == '/get_friend_list': data = [{'user_id':123}] if friends[0] else []
        else:
            assert request.url.path == '/send_private_msg'
            writes.append(json.loads(request.content));data={'message_id':789}
        return httpx.Response(200,json={'status':'ok','retcode':0,'data':data})
    adapter = QQAdapter({**DEFAULTS,'robot_id':'456','access_token':'fake','callback_secret':'fake'},s,'owner',
        client=httpx.Client(base_url='http://onebot.test',transport=httpx.MockTransport(handle)))
    settings = AutonomySettings(private_enabled=True, windows=(('00:00','23:59'),),**policy)
    target = AutonomyTarget('qq','456',settings,activity=adapter.pending_private_activity,scope='qq')
    e.autonomy.targets=(target,)
    e.private_access['qq']=adapter;e.sinks['qq']=adapter
    e.private_policies['qq']={'multilingual':True,'topic_filter':False}
    e.memory_service.shared_owner_private=True
    proposal={'decision':'send','text':'刚才聊的那个话题，吾辈又有个小想法。','delay_minutes':10,'can_send':True,'review_reason':'有依据的自然接话'}
    def decide(operation,payload):
        m.calls.append((operation,deepcopy(payload)))
        if operation=='autonomous_private':return deepcopy(proposal)
        if operation=='review_action':return {'decision':'approve','reason':'可发送'}
        raise AssertionError(operation)
    m.decide=decide
    return e,s,m,adapter,proposal,writes,friends


def plan(store,platform='qq'):
    return next(Action.from_dict(json.loads(r['payload'])) for r in store.inspect('actions')
                if json.loads(r['payload'])['platform']==platform)


def incoming(adapter,now,key='incoming'):
    return Event(key,'qq','456','123',adapter.private_conversation('456','123'),'private_message','还有一件事',now,'private')


def test_context_uses_shared_owner_memory_and_acceptance_is_archived(factory,now):
    e,s,m,a,proposal,writes,_=setup(factory)
    try:
        for event in [Event('qq-old','qq','456','123',a.private_conversation('456','123'),'private_message','想练习画画',now-7200,'private'),
                      Event('bili-old','bilibili','robot','424242','bili-owner','private_message','今天研究了构图',now-7100,'private'),
                      Event('stranger','bilibili','robot','999','other-private','private_message','不可混入的私事',now-7000,'private')]:
            e.ingest(event);s.finish_event(event.event_id,'done')
        proposal['text']='構図の話、吾辈又想起一件事。';proposal['recipient_id']='999'
        assert e.autonomy.run(now)
        payload=m.calls[0][1];history=json.dumps(payload['history'],ensure_ascii=False)
        assert '想练习画画' in history and '研究了构图' in history and '不可混入' not in history
        assert payload['private_policy']['multilingual']
        action=plan(s)
        assert action.recipient_id=='123' and action.conversation_id==a.private_conversation('456','123')
        assert not e.dispatch_one(now+599) and not writes
        assert e.dispatch_one(now+600) and writes[0]['user_id']==123
        records=[r for r in s.inspect('conversation_records') if r['role']=='agent']
        assert len(records)==1 and records[0]['platform']=='qq' and records[0]['person_id']=='owner'
        assert records[0]['text']==proposal['text']
    finally:a.close()


def test_debounce_buffer_blocks_generation_and_invalidates_scheduled_opening(factory,now):
    e,s,m,a,_,writes,_=setup(factory)
    try:
        assert e.autonomy.run(now)
        event=replace(incoming(a,now+1),conversation_id='qq:456:123')
        assert ChatBatches(s).receive(event,s.binding('owner','qq'),now+1)
        assert not s.inspect('events')  # Not flushed into ordinary processing yet.
        assert e.dispatch_one(now+600) and not writes
        assert s.inspect('actions')[0]['state']=='failed'
        assert not e.autonomy.run(now+14400)
        assert len(m.calls)==1
    finally:a.close()


@pytest.mark.parametrize('change',['friend','binding','memory','new_message'])
def test_plan_rechecks_sources_and_private_authorization(factory,now,change):
    e,s,m,a,_,writes,friends=setup(factory)
    try:
        assert e.autonomy.run(now)
        if change=='friend':friends[0]=False
        elif change=='binding':s.revoke_binding('owner','qq')
        elif change=='memory':s.set_setting('memory_epoch',s.get_setting('memory_epoch',0)+1)
        else:e.ingest(incoming(a,now+1))
        e.dispatch_one(now+600)
        assert not writes and not any(r['role']=='agent' for r in s.inspect('conversation_records'))
    finally:a.close()


def test_no_friend_no_model_and_skip_spends_decision_budget(factory,now):
    e,s,m,a,proposal,_,friends=setup(factory,daily_decision_limit=1)
    try:
        friends[0]=False
        assert not e.autonomy.run(now) and not m.calls
        friends[0]=True;proposal['decision']='skip'
        assert not e.autonomy.run(now+1800)
        assert not e.autonomy.run(now+3600)
        assert len(m.calls)==1 and not s.inspect('actions')
        assert s.get_setting('autonomy_status:qq:private')['state']=='budget_exhausted'
    finally:a.close()


def test_rejected_inline_output_requires_human_review(factory,now):
    e,s,m,a,proposal,writes,_=setup(factory)
    try:
        proposal['can_send']=False
        assert e.autonomy.run(now) and s.inspect('actions')[0]['state']=='draft'
        assert not e.dispatch_one(now+600,pre_reviewed_only=True) and not writes
    finally:a.close()


def test_unknown_qq_write_is_not_replayed_or_replanned(factory,now):
    e,s,m,a,_,writes,_=setup(factory)
    def timeout(request):
        if request.url.path=='/get_login_info':data={'user_id':456}
        elif request.url.path=='/get_friend_list':data=[{'user_id':123}]
        else:
            writes.append('attempt');raise httpx.ReadTimeout('unknown')
        return httpx.Response(200,json={'status':'ok','retcode':0,'data':data})
    a.client.close();a.client=httpx.Client(base_url='http://onebot.test',transport=httpx.MockTransport(timeout))
    try:
        assert e.autonomy.run(now) and e.dispatch_one(now+600)
        assert s.inspect('actions')[0]['state']=='unknown'
        assert not e.autonomy.run(now+86400) and not e.dispatch_one(now+86400)
        assert writes==['attempt'] and len(m.calls)==1
    finally:a.close()


def test_qq_and_bili_policies_budgets_pending_and_pause_are_separate(factory,now):
    e,s,m,a,_,writes,_=setup(factory,daily_decision_limit=1)
    try:
        qq=e.autonomy.targets[0]
        bili=AutonomyTarget('bilibili','robot',qq.settings)
        e.autonomy.targets=(bili,qq)
        e.private_access['bilibili']=SimpleNamespace(allows_private_reply=lambda *args:True,private_conversation=lambda *args:'bili-owner')
        s.set_setting('platform_paused:bilibili',True)
        assert e.autonomy.run(now) and len(s.inspect('actions'))==1
        s.set_setting('platform_paused:bilibili',False)
        assert e.autonomy.run(now+1)
        assert {json.loads(r['payload'])['platform'] for r in s.inspect('actions')}=={'qq','bilibili'}
        assert len(m.calls)==2  # QQ's pending plan/budget does not consume Bili's.
        assert s.get_setting('autonomy_status:private')['state']=='planned'
        assert s.get_setting('autonomy_status:qq:private')['state']=='planned'
        # A QQ-only plan does not make Bili polling enter its quiet window.
        assert s.polling_quiet(now+600,60,60,platform='qq')
        s.cancel_autonomy(platform='bilibili')
        from murasamebot.adapters.request_monitor import RequestMonitor
        from murasamebot.application.request_policy import RequestSettings
        assert RequestMonitor(s,RequestSettings()).allowed('exploration',now+600)
    finally:a.close()


@pytest.mark.parametrize('settings',[{'private_enabled':'yes'},{'private_idle_minutes':0},
    {'windows':[{'start':'25:00','end':'22:00'}]},{'weekdays':[True]}, {'dynamic_enabled':True}])
def test_qq_autonomy_configuration_is_validated_without_changing_bili(tmp_path,settings):
    from test_architecture_and_config import write_config
    from murasamebot.bootstrap.config import Config
    path=write_config(tmp_path);before=Config(path).data['autonomy']
    with pytest.raises(ValueError):Config(path).save_local({'qq_autonomy':settings})
    assert Config(path).data['autonomy']==before


def test_new_debounce_input_between_precheck_and_http_write_blocks_send(factory,now):
    e,s,m,a,_,writes,_=setup(factory)
    try:
        assert e.autonomy.run(now)
        original=a.api
        def api(action,payload=None,**kwargs):
            if action=='get_friend_list':
                ChatBatches(s).receive(replace(incoming(a,now+600),conversation_id='qq:456:123'),s.binding('owner','qq'),now+600)
            return original(action,payload,**kwargs)
        a.api=api
        assert e.dispatch_one(now+600) and not writes
        assert s.inspect('actions')[0]['state']=='failed'
    finally:a.close()


def runtime_config(tmp_path):
    import yaml
    from test_architecture_and_config import write_config
    from murasamebot.bootstrap.config import Config
    path=write_config(tmp_path)
    local={'platforms':{'qq':{'enabled':True},'bilibili':{'enabled':False}},
        'bilibili':{'private_messages_enabled':False},
        'qq':{'robot_id':'456','access_token':'fake','callback_secret':'fake'},
        'bindings':[{'person_id':'owner','platform':'qq','external_id':'123','purpose':'owner_video_recommendation'}],
        'qq_autonomy':{'private_enabled':True,'windows':[{'start':'00:00','end':'23:59'}]}}
    Config(path).local_path.write_text(yaml.safe_dump(local),encoding='utf-8')
    return path


def mock_runtime_adapter(monkeypatch,writes):
    import murasamebot.adapters.platforms.qq as qq
    def create(settings,store,owner):
        def handle(request):
            if request.url.path=='/get_login_info':data={'user_id':456}
            elif request.url.path=='/get_friend_list':data=[{'user_id':123}]
            else:writes.append(json.loads(request.content));data={'message_id':789}
            return httpx.Response(200,json={'status':'ok','retcode':0,'data':data})
        return QQAdapter(settings,store,owner,client=httpx.Client(base_url='http://onebot.test',transport=httpx.MockTransport(handle)))
    monkeypatch.setattr(qq,'QQAdapter',create)


def test_runtime_qq_only_plan_survives_restart_and_bili_private_switch(tmp_path,now,monkeypatch):
    from murasamebot.bootstrap.config import Config
    from murasamebot.bootstrap.runtime import build
    path=runtime_config(tmp_path);writes=[];mock_runtime_adapter(monkeypatch,writes)
    runtime=build(Config(path))
    try:
        runtime.engine.model=SimpleNamespace(decide=lambda op,payload:{'decision':'send','text':'吾辈想接着聊一会儿。','delay_minutes':10,'can_send':True,'review_reason':'自然接话'})
        assert runtime.engine.autonomy.run(now)
        old=plan(runtime.store)
        assert old.kind=='proactive_private' and old.platform=='qq'
    finally:runtime.close()
    restarted=build(Config(path))
    try:
        assert not restarted.engine.dispatch_one(now+599)
        assert restarted.engine.dispatch_one(now+600)
        assert len(writes)==1 and writes[0]['user_id']==123
        assert not restarted.engine.autonomy.run(now+700)
    finally:restarted.close()


def test_runtime_policy_change_blocks_old_plan_and_cancels_without_revival(tmp_path,now,monkeypatch):
    from murasamebot.bootstrap.config import Config
    from murasamebot.bootstrap.runtime import build,prepare_store
    path=runtime_config(tmp_path);writes=[];mock_runtime_adapter(monkeypatch,writes)
    runtime=build(Config(path))
    try:
        runtime.engine.model=SimpleNamespace(decide=lambda op,payload:{'decision':'send','text':'吾辈又有个小想法。','delay_minutes':10,'can_send':True,'review_reason':'自然接话'})
        assert runtime.engine.autonomy.run(now)
        old=plan(runtime.store)
        bili=Action('bili-kept',runtime.store.agent_id,'bilibili','robot',None,'原B站计划',now,now+3600,
            kind='autonomous_post',not_before=now,autonomy_version=Config(path).behaviour().autonomy.version())
        assert runtime.store.enqueue_action(bili,draft=True,now=now)
        cfg=Config(path);cfg.save_local({'qq_autonomy':{'private_enabled':False}})
        assert runtime.engine.action_policy(old,now+600)
        prepare_store(Config(path))
        states={row['action_id']:row['state'] for row in runtime.store.inspect('actions')}
        assert states[old.action_id]=='cancelled' and states['bili-kept']=='draft'
        Config(path).save_local({'qq_autonomy':{'private_enabled':True}})
        prepare_store(Config(path))
        states={row['action_id']:row['state'] for row in runtime.store.inspect('actions')}
        assert states[old.action_id]=='cancelled' and states['bili-kept']=='draft' and not writes
    finally:runtime.close()


def test_bili_background_read_gate_does_not_block_qq_initiative(tmp_path,now,monkeypatch):
    from murasamebot.bootstrap.config import Config
    from murasamebot.bootstrap.runtime import build
    from murasamebot.bootstrap.platforms import PlatformModule
    from murasamebot.bootstrap.platforms.qq import build_module
    path=runtime_config(tmp_path)
    Config(path).save_local({'platforms':{'bilibili':{'enabled':True}}})
    writes=[];mock_runtime_adapter(monkeypatch,writes)
    adapter=SimpleNamespace(account_id='robot',configured=True,close=lambda:None)
    factories={'qq':build_module,'bilibili':lambda c,s:PlatformModule('bilibili',adapter,
        frozenset({'proactive_private'}),monitor=SimpleNamespace(allowed=lambda *args:False))}
    runtime=build(Config(path),platform_factories=factories)
    calls=[]
    def decide(op,payload):
        calls.append(op)
        return {'decision':'send','text':'吾辈想起之前聊的话题。','delay_minutes':10,'can_send':True,'review_reason':'自然接话'}
    runtime.engine.model=SimpleNamespace(decide=decide)
    try:
        runtime.run_background(now)
        assert calls==['autonomous_private'] and plan(runtime.store).platform=='qq'
        assert not writes
    finally:runtime.close()


def test_console_exposes_qq_schedule_and_saves_only_qq_policy(console):
    path,s,client=console
    before=client.get('/api/config').json()['autonomy']
    response=client.post('/api/config',json={'qq_autonomy':{'private_enabled':True,'private_daily_limit':3}})
    assert response.status_code==200,response.text
    data=client.get('/api/platforms/qq').json()
    assert data['autonomy']['settings']['private_enabled'] and data['autonomy']['settings']['private_daily_limit']==3
    assert client.get('/api/config').json()['autonomy']==before
    html=client.get('/').text
    assert 'id="qq-initiative-enabled"' in html and 'id="qq-autonomy-status"' in html
