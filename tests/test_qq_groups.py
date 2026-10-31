"""Real core + SQLite + fake OneBot; never access a real group/account."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import hmac
import json
import time
from types import SimpleNamespace

import httpx
import pytest

from murasamebot.adapters.persistence.group_inbox import GroupInbox
from murasamebot.adapters.platforms.qq import QQAdapter
from murasamebot.application.group_chat import DEFAULTS, GroupChat, validate
from murasamebot.bootstrap.platforms.qq import DEFAULTS as QQ_DEFAULTS
from murasamebot.domain.models import Action, Event, stable_id


def raw(mid=1, peer=321, group=789, *, at='456', quote=None, text='这段话是什么意思？', **extra):
    message=[]
    if quote is not None:message.append({'type':'reply','data':{'id':str(quote)}})
    if at is not None:message.append({'type':'at','data':{'qq':at}})
    message.append({'type':'text','data':{'text':text}})
    return {'post_type':'message','message_type':'group','sub_type':'normal','self_id':456,
        'group_id':group,'user_id':peer,'message_id':mid,'sender':{'user_id':peer,'nickname':'群友'},'message':message,**extra}


@pytest.fixture
def chat(factory,monkeypatch):
    e,s,m,p=factory(model_review=True,review_strategy='inline')
    s.bind('owner','qq','123')
    policy=deepcopy(DEFAULTS);policy.update(enabled=True,group_ids=['789','790'])
    state={'writes':[],'calls':[],'quotes':{},'muted':False,'membership':True,'timeout':False,'reject':False,'clock':time.time()}
    import murasamebot.adapters.platforms.qq as qq_module
    monkeypatch.setattr(qq_module,'time',SimpleNamespace(time=lambda:state['clock']))
    def transport(request):
        path=request.url.path;body=json.loads(request.content);state['calls'].append((path,body))
        if path=='/get_login_info':data={'user_id':456}
        elif path=='/get_group_member_info':
            data={**body,'shut_up_timestamp':time.time()+600 if state['muted'] and body['user_id']==456 else 0}
            if not state['membership']:data['user_id']=999
        elif path=='/get_msg':data=state['quotes'].get(body['message_id'])
        elif path=='/send_group_msg':
            state['writes'].append(body)
            if state['timeout']:raise httpx.ReadTimeout('ambiguous')
            if state['reject']:return httpx.Response(200,json={'status':'failed','retcode':100})
            data={'message_id':1000+len(state['writes'])}
        else:raise AssertionError('unexpected API: '+path)
        return httpx.Response(200,json={'status':'ok','retcode':0,'data':data})
    a=QQAdapter({**QQ_DEFAULTS,'robot_id':'456','access_token':'fake','callback_secret':'fake'},s,'owner',
        client=httpx.Client(base_url='http://qq.test',transport=httpx.MockTransport(transport)))
    a.group_settings=policy;inbox=GroupInbox(s)
    e.sinks['qq']=a;e.routing=lambda:(('qq','456'),('bilibili','robot'))
    e.event_handlers['group_message']=GroupChat(e,policy,inbox,a).handle
    e.action_policy=lambda action,now:inbox.guard(action,policy,now) if action.kind=='group_reply' else ''
    calls=[]
    def decide(op,payload):
        calls.append((op,deepcopy(payload)))
        assert op in {'group_chat','review_action'}
        if op=='review_action':return {'decision':'approve','reason':'合成审核通过'}
        return {'decision':'reply','text':'这句话是在说刚才讨论的事情。','topic_class':'casual','can_send':True,'review_reason':'合成正文通过'}
    m.decide=decide
    yield e,s,a,inbox,state,calls,time.time()
    a.close()


def reply(chat,payload=None,at=None):
    e,s,a,inbox,state,calls,now=chat
    when=now if at is None else at
    state['clock']=when
    a.parse(payload or raw(),when)
    assert e.process_one_event(when)
    return s.inspect('actions')


def test_recent_context_authorship_and_single_directed_reply(chat):
    e,s,a,inbox,state,calls,now=chat
    a.parse(raw(10,peer=123,at=None,text='刚才在讨论画画。'),now-3)
    a.parse(raw(11,peer=999,at=None,text='我说的是水彩。'),now-2)
    assert not s.inspect('events') and not calls
    rows=reply(chat)
    context=calls[0][1]['context']
    assert [r['user_id'] for r in context]==['123','999','321']
    assert calls[0][1]['target_user_id']=='321'
    assert len(rows)==1 and rows[0]['state']=='queued'
    assert e.dispatch_one(now)
    assert state['writes']==[{'group_id':789,'message':[
        {'type':'reply','data':{'id':'1'}},{'type':'at','data':{'qq':'321'}},
        {'type':'text','data':{'text':' 这句话是在说刚才讨论的事情。'}}]}]
    assert not e.dispatch_one(now+40)
    records=s.inspect('conversation_records')
    assert len(records)==2 and {r['purpose'] for r in records}=={'group_dialogue'}
    assert {r['visibility'] for r in records}=={'group'}
    a.parse(raw(2,at=None),now+1)
    context=GroupInbox(s).context(s.event(json.loads(rows[0]['payload'])['event_id']),a.group_settings,now+1)
    assert any(m.kind=='group_outgoing' for m in context)


@pytest.mark.parametrize('changes',[
    {'self_id':999},{'group_id':999},{'sub_type':'anonymous'},{'sub_type':'notice'},
    {'user_id':456},{'sender':{'user_id':999}},{'message_id':True},
])
def test_untrusted_or_unconfigured_group_does_not_enter_context(chat,changes):
    e,s,a,inbox,state,calls,now=chat
    a.parse(raw(**changes),now)
    assert not s.inspect('events') and not calls and not state['writes']
    with s.connection() as db:assert not db.execute('SELECT * FROM group_inbox').fetchall()


@pytest.mark.parametrize('at',[None,'all','123'])
def test_other_people_chat_and_at_all_are_context_only(chat,at):
    e,s,a,inbox,state,calls,now=chat
    a.parse(raw(at=at),now)
    assert not e.process_one_event(now) and not calls
    with s.connection() as db:assert db.execute('SELECT COUNT(*) FROM group_inbox').fetchone()[0]==1


@pytest.mark.parametrize('author,group,kind,triggered',[(456,789,'group',True),(999,789,'group',False),
    (456,790,'group',False),(456,789,'private',False)])
def test_quote_trigger_must_be_bot_in_same_group(chat,author,group,kind,triggered):
    e,s,a,inbox,state,calls,now=chat
    state['quotes'][50]={'message_type':kind,'group_id':group,'message_id':50,
        'sender':{'user_id':author},'message':[{'type':'text','data':{'text':'引用的内容'}}]}
    a.parse(raw(at=None,quote=50),now)
    assert e.process_one_event(now) is triggered
    assert bool(calls) is triggered
    if triggered:
        assert calls[0][1]['quoted_message']['user_id']=='456'
        assert calls[0][1]['target_user_id']=='321'
        assert e.dispatch_one(now)
        assert state['writes'][0]['message'][0]['data']['id']=='1' # incoming, not quoted 50


def test_quoted_author_is_not_the_answer_target(chat):
    e,s,a,inbox,state,calls,now=chat
    state['quotes'][50]={'message_type':'group','group_id':789,'message_id':50,
        'sender':{'user_id':123,'nickname':'主人'},'message':[{'type':'text','data':{'text':'上一条'}}]}
    reply(chat,raw(quote=50))
    payload=calls[0][1]
    assert payload['target_user_id']=='321' and payload['quoted_message']['user_id']=='123'
    assert payload['owner_addressed'] is False


def test_duplicate_callback_and_restart_cannot_replay_reply(chat):
    e,s,a,inbox,state,calls,now=chat
    a.parse(raw(),now);a.parse(raw(),now+1)
    assert e.process_one_event(now+1) and not e.process_one_event(now+2)
    assert e.dispatch_one(now+2)
    a.parse(raw(),now+2000) # raw cache was pruned; durable event ID still protects
    assert not e.process_one_event(now+2000) and len(state['writes'])==1


@pytest.mark.parametrize('peer,expected',[(123,True),(321,False)])
def test_owner_binding_not_nickname_and_no_private_sources(chat,peer,expected):
    e,s,a,inbox,state,calls,now=chat
    e.ingest(Event('secret','qq','456','123','private-owner','private_message','PRIVATE-SECRET',now-1,'private'))
    s.finish_event('secret','done')
    reply(chat,raw(peer=peer,sender={'user_id':peer,'nickname':'主人'}))
    payload=calls[0][1]
    assert payload['owner_addressed'] is expected
    assert 'PRIVATE-SECRET' not in json.dumps(payload,ensure_ascii=False)
    assert e.persona.owner_style not in payload['persona']


def test_group_context_scope_and_window_survive_reconstruction(chat):
    e,s,a,inbox,state,calls,now=chat
    a.group_settings['context_messages']=3
    for mid in range(10,15):a.parse(raw(mid,at=None,text=str(mid)),now-10+mid/10)
    a.parse(raw(20,group=790,at=None,text='OTHER-GROUP'),now-1)
    a.parse(raw(),now)
    event=s.event(stable_id('qq-group','456','789','1'))
    context=GroupInbox(s).context(event,a.group_settings,now)
    assert [m.text for m in context][:-1]==['13','14']
    assert 'OTHER-GROUP' not in str(context)
    assert not GroupInbox(s).context(event,a.group_settings,now+1801)


@pytest.mark.parametrize('change',['disable','remove','revision','reset','forget','mute','membership'])
def test_scope_and_permissions_are_checked_again_before_write(chat,change):
    e,s,a,inbox,state,calls,now=chat
    reply(chat)
    if change=='disable':a.group_settings['enabled']=False
    if change=='remove':a.group_settings['group_ids']=['790']
    if change=='revision':a.group_settings['max_characters']=200
    if change=='reset':inbox.reset()
    if change=='forget':s.forget_source(stable_id('qq-group','456','789','1'))
    if change=='mute':state['muted']=True
    if change=='membership':state['membership']=False
    e.dispatch_one(now)
    assert not state['writes']


def test_last_callback_can_stop_group_post(chat):
    e,s,a,inbox,state,calls,now=chat
    reply(chat)
    action=Action.from_dict(json.loads(s.inspect('actions')[0]['payload']))
    assert a.send(action,{},before_write=lambda:False).state=='failed'
    assert not state['writes']


def test_ambiguous_write_is_not_replayed_and_only_blocks_its_group(chat):
    e,s,a,inbox,state,calls,now=chat
    state['timeout']=True
    reply(chat);assert e.dispatch_one(now)
    assert s.inspect('actions')[0]['state']=='unknown'
    reply(chat,raw(2),now+60)
    assert not e.dispatch_one(now+60) and len(state['writes'])==1
    state['timeout']=False
    reply(chat,raw(3,group=790),now+61)
    assert e.dispatch_one(now+61) and len(state['writes'])==2


def test_cooldown_and_rolling_daily_limit(chat):
    e,s,a,inbox,state,calls,now=chat
    a.group_settings['daily_reply_limit']=2
    reply(chat);assert e.dispatch_one(now)
    reply(chat,raw(2),now+10)
    assert not e.dispatch_one(now+10)
    reply(chat,raw(3),now+31);assert e.dispatch_one(now+31) and len(state['writes'])==2
    reply(chat,raw(4),now+70)
    assert not e.dispatch_one(now+70) and len(state['writes'])==2


def test_model_skip_and_failed_inline_review(chat):
    e,s,a,inbox,state,calls,now=chat
    e.model.decide=lambda op,p:{'decision':'skip'}
    reply(chat);assert not s.inspect('actions')
    e.model.decide=lambda op,p:{'decision':'reply','text':'这句话是说画画。','topic_class':'casual','can_send':False,'review_reason':'待人工核对'}
    rows=reply(chat,raw(2),now+40)
    assert len(rows)==1 and rows[0]['state']=='draft'
    assert not e.dispatch_one(now+40) and not state['writes']


def test_target_override_cannot_route_elsewhere_or_create_task(chat):
    e,s,a,inbox,state,calls,now=chat
    e.model.decide=lambda op,p:{'decision':'reply','text':'好的，收到。','topic_class':'casual',
        'can_send':True,'review_reason':'合成审核','group_id':'999','recipient_id':'999','task':{'kind':'request'}}
    reply(chat);assert e.dispatch_one(now)
    assert state['writes'][0]['group_id']==789
    assert state['writes'][0]['message'][1]['data']['qq']=='321'
    assert not s.inspect('tasks')


def test_memory_epoch_race_discards_generated_group_reply(chat):
    e,s,a,inbox,state,calls,now=chat
    def decide(op,p):
        s.set_setting('memory_epoch',s.get_setting('memory_epoch',0)+1)
        return {'decision':'reply','text':'过时正文','topic_class':'casual','can_send':True}
    e.model.decide=decide
    reply(chat)
    assert not s.inspect('actions') and not state['writes']


def test_group_archive_cannot_reach_private_or_public_initiative_context(chat):
    e,s,a,inbox,state,calls,now=chat
    reply(chat,raw(text='GROUP-ONLY-CONTEXT'));assert e.dispatch_one(now)
    e.memory_service.shared_owner_private=True
    private=Event('private','qq','456','123','private-owner','private_message','群里聊了什么？',now+40,'private')
    for context in [e.memory_service.context(private,'owner',True,now+40),e.memory_service.public_initiative_context(now+40)]:
        text=json.dumps(context,ensure_ascii=False)
        assert 'GROUP-ONLY-CONTEXT' not in text and '这句话是在说' not in text


def test_independent_review_uses_group_source_without_owner_relationship(chat):
    e,s,a,inbox,state,calls,now=chat
    e.settings=replace(e.settings,review_strategy='independent')
    reply(chat,raw(peer=123))
    assert e.dispatch_one(now)
    reviewed=[p for op,p in calls if op=='review_action']
    assert reviewed and e.persona.owner_style not in reviewed[0]['persona']
    assert reviewed[0]['action_kind']=='group_reply'


def test_natural_foreign_text_and_unanalysed_group_images(chat):
    e,s,a,inbox,state,calls,now=chat
    e.model.decide=lambda op,p:{'decision':'reply','text':'OK，刚才说的就是这个。','topic_class':'casual','can_send':True,'review_reason':'合成通过'}
    payload=raw();payload['message'].append({'type':'image','data':{'url':'https://invalid/image'}})
    reply(chat,payload)
    assert e.dispatch_one(now) and 'OK' in state['writes'][0]['message'][-1]['data']['text']
    assert not any('/image' in path for path,body in state['calls'])


def test_gateway_does_not_truncate_latest_group_target_after_twenty_messages(factory):
    from murasamebot.adapters.models.chat_completions import ChatCompletionsModel
    e,s,m,p=factory()
    def transport(request):
        body=json.loads(request.content);payload=json.loads(body['messages'][1]['content'])
        assert len(payload['context'])==30 and payload['context'][-1]['text']=='29'
        return httpx.Response(200,json={'choices':[{'message':{'content':'{"decision":"skip"}'}}]})
    gateway=ChatCompletionsModel(base_url='https://model.invalid',name='fake',api_key='',store=s,
        timezone='Asia/Hong_Kong',daily_limit=1,client=httpx.Client(transport=httpx.MockTransport(transport)))
    try:assert gateway.decide('group_chat',{'context':[{'text':str(i)} for i in range(30)]})['decision']=='skip'
    finally:gateway.close()


@pytest.mark.parametrize('blocked_state',['muted','membership'])
def test_member_check_runs_before_generation(chat,blocked_state):
    e,s,a,inbox,state,calls,now=chat
    state[blocked_state]=False if blocked_state=='membership' else True
    reply(chat)
    assert not calls and not s.inspect('actions') and not state['writes']


def test_direct_at_handles_unknown_quote_without_guessing(chat):
    e,s,a,inbox,state,calls,now=chat
    reply(chat,raw(quote=999))
    assert calls[0][1]['quoted_message'] is None
    assert e.dispatch_one(now)


def test_memory_clear_also_invalidates_unarchived_context(chat):
    e,s,a,inbox,state,calls,now=chat
    a.parse(raw(10,at=None,text='OLD-CACHED-CONTEXT'),now-1)
    s.set_setting('memory_epoch',s.get_setting('memory_epoch',0)+1)
    reply(chat)
    assert 'OLD-CACHED-CONTEXT' not in json.dumps(calls[0][1],ensure_ascii=False)


def test_forgetting_source_prevents_quote_reloading_old_bot_reply(chat):
    e,s,a,inbox,state,calls,now=chat
    reply(chat);assert e.dispatch_one(now)
    s.forget_source(stable_id('qq-group','456','789','1'))
    state['quotes'][1001]={'message_type':'group','group_id':789,'message_id':1001,
        'sender':{'user_id':456},'message':[{'type':'text','data':{'text':'已被遗忘的旧回答'}}]}
    assert a.group_quote('789','1001') is None
    assert not any(path=='/get_msg' for path,body in state['calls'])


def test_real_composition_routes_group_reply_and_projects_author(tmp_path,monkeypatch):
    from test_receivers import config_path
    from murasamebot.bootstrap.config import Config
    from murasamebot.bootstrap.runtime import build
    from murasamebot.bootstrap.console import platform_console
    import murasamebot.adapters.platforms.qq as qq_module
    path=config_path(tmp_path)
    Config(path).save_local({'platforms':{'qq':{'enabled':True},'bilibili':{'enabled':False}},
        'qq_groups':{'enabled':True,'group_ids':['789']},'mode':'reviewed'})
    original=qq_module.QQAdapter;writes=[]
    def factory(settings,store,owner):
        def transport(request):
            body=json.loads(request.content)
            if request.url.path=='/get_login_info':data={'user_id':456}
            elif request.url.path=='/get_group_member_info':data=body
            elif request.url.path=='/send_group_msg':writes.append(body);data={'message_id':987}
            else:raise AssertionError('unexpected API')
            return httpx.Response(200,json={'status':'ok','retcode':0,'data':data})
        return original(settings,store,owner,client=httpx.Client(base_url='http://qq.test',transport=httpx.MockTransport(transport)))
    monkeypatch.setattr(qq_module,'QQAdapter',factory)
    runtime=build(Config(path))
    try:
        e=runtime.engine;now=time.time()
        e.model.decide=lambda op,p:{'decision':'reply','text':'吾辈明白了。','topic_class':'casual','can_send':True,'review_reason':'合成审核'}
        runtime.platform.parse(raw(sender={'user_id':321,'nickname':'群友 <b>同学</b>'}),now)
        assert e.process_one_event(now) and e.dispatch_one(now)
        records=platform_console(Config(path),'qq')['groups']['records']
        assert len(records)==2 and {r['group_id'] for r in records}=={'789'}
        assert {r['speaker_id'] for r in records}=={'321','456'}
        assert not platform_console(Config(path),'qq')['records']
        assert len(writes)==1
    finally:runtime.close()


@pytest.mark.parametrize('bad',[{'enabled':True,'group_ids':[]},{'enabled':'true'},
    {'group_ids':['123','123']},{'group_ids':[123]},{'context_messages':True},{'context_messages':101},
    {'cooldown_seconds':0},{'group_ids':['１２３']},{'unexpected':1}])
def test_invalid_group_policy(bad):
    with pytest.raises(ValueError):validate({**deepcopy(DEFAULTS),**bad})


def test_config_save_is_scoped_and_dashboard_projects_group_settings(tmp_path):
    from test_receivers import config_path
    from murasamebot.bootstrap.config import Config
    from murasamebot.bootstrap.runtime import prepare_store
    from murasamebot.bootstrap.console import platform_console
    path=config_path(tmp_path);cfg=Config(path)
    assert cfg.data['qq_groups']['enabled'] is False
    previous=deepcopy(cfg.data)
    cfg.save_local({'qq_groups':{'enabled':True,'group_ids':['789']}})
    updated=Config(path)
    for key in ('qq','bilibili','qq_autonomy','qq_space','bindings'):
        assert updated.data[key]==previous[key]
    s=prepare_store(updated)
    assert platform_console(updated,'qq')['groups']['settings']['group_ids']==['789']
    assert not platform_console(updated,'qq')['groups']['records']
    updated.save_local({'qq_groups':{'enabled':False}})
    prepare_store(Config(path))


def test_signed_receiver_commits_group_event_without_private_batching(tmp_path,monkeypatch):
    from test_receivers import config_path,IdleWorker
    from murasamebot.bootstrap.config import Config
    from murasamebot.bootstrap.receivers import ReceiverManager
    from murasamebot.bootstrap.runtime import prepare_store
    import murasamebot.bootstrap.qq_receiver as receiver
    monkeypatch.setattr(receiver,'QQAdapter',lambda settings,store,owner:QQAdapter(settings,store,owner,
        client=httpx.Client(base_url='http://qq.test',transport=httpx.MockTransport(lambda r:pytest.fail('receiver must not generate/send')))))
    path=config_path(tmp_path)
    Config(path).save_local({'qq_groups':{'enabled':True,'group_ids':['789']}})
    manager=ReceiverManager(path,worker_target=IdleWorker())
    manager.control('qq','start')
    body=json.dumps(raw()).encode();sig='sha1='+hmac.new(b'test-signature',body,hashlib.sha1).hexdigest()
    try:
        with httpx.Client(trust_env=False) as c:
            url=f'http://127.0.0.1:{Config(path).data["qq"]["callback_port"]}/onebot/qq'
            assert c.post(url,content=body).status_code==401
            for _ in range(2):assert c.post(url,content=body,headers={'X-Signature':sig}).status_code==200
        s=prepare_store(Config(path))
        assert len(s.inspect('events'))==1
        assert s.inspect('events')[0]['state']=='pending'
        assert s.platform_console('qq')['buffered']['messages']==0
    finally:manager.close()
