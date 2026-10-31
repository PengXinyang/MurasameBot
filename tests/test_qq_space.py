from copy import deepcopy
from dataclasses import replace
import json

import httpx
import pytest

from murasamebot.application.autonomy import AutonomySettings,AutonomyTarget
from murasamebot.adapters.platforms.qq import QQAdapter
from murasamebot.bootstrap.platforms.qq import DEFAULTS
from murasamebot.domain.models import Action,Event
from test_platform_console import console


def setup(factory,mode='accepted'):
    e,s,m,b=factory(model_review=True,review_strategy='inline')
    writes=[]
    def handle(req):
        if req.url.path=='/get_login_info':data={'user_id':456}
        else:
            assert req.url.path=='/send_qzone_msg';writes.append(json.loads(req.content))
            if mode=='timeout':raise httpx.ReadTimeout('unknown')
            if mode=='failed':return httpx.Response(200,json={'status':'failed','retcode':1})
            data={'tid':'space-123'} if mode=='accepted' else {}
        return httpx.Response(200,json={'status':'ok','retcode':0,'data':data})
    a=QQAdapter({**DEFAULTS,'robot_id':'456','access_token':'fake','callback_secret':'fake'},s,'owner',
        client=httpx.Client(base_url='http://napcat.test',transport=httpx.MockTransport(handle)))
    policy=AutonomySettings(dynamic_enabled=True,windows=(('00:00','23:59'),))
    e.autonomy.targets=(AutonomyTarget('qq','456',policy,scope='qq:space',channels=('dynamic',)),)
    e.sinks['qq']=a
    result={'decision':'send','text':'之前聊起的构图，吾辈又有个小想法。','delay_minutes':10,'can_send':True,'review_reason':'可公开表达','topic_class':'casual'}
    def decide(op,payload):m.calls.append((op,deepcopy(payload)));return deepcopy(result)
    m.decide=decide
    return e,s,m,a,result,writes


def test_space_uses_public_memories_current_time_and_no_private_context(factory,now):
    e,s,m,a,r,w=setup(factory)
    try:
        for vis,text,key in [('public','公开谈过的构图','public'),('private','绝不可外发的私人原文','private')]:
            event=Event(key,'bilibili','robot','424242',key,'reply',text,now-3600,vis)
            e.ingest(event);s.finish_event(key,'done')
        assert e.autonomy.run(now)
        op,payload=m.calls[0]
        assert op=='autonomous_post' and payload['current_time']
        assert '公开谈过的构图' in json.dumps(payload,ensure_ascii=False)
        assert '绝不可外发' not in json.dumps(payload,ensure_ascii=False)
        assert not e.dispatch_one(now+599)
        assert e.dispatch_one(now+600)
        assert w==[{'content':r['text'],'images':[],'ugc_right':4,'target_uins':[]}]
        records=[x for x in s.inspect('conversation_records') if x['role']=='agent']
        assert len(records)==1 and records[0]['visibility']=='public' and records[0]['platform']=='qq'
        assert r['text'] in json.dumps(e.memory_service.public_initiative_context(now+601),ensure_ascii=False)
        s.bind('owner','qq','123')
        probe=Event('probe','qq','456','123',a.private_conversation('456','123'),'context_probe','刚才说说',now+601,'private')
        assert r['text'] in json.dumps(e.memory_service.context(probe,'owner',True,now+601),ensure_ascii=False)
    finally:a.close()


@pytest.mark.parametrize('mode,state',[('timeout','unknown'),('empty','unknown'),('failed','failed')])
def test_space_receipts_are_conservative_and_not_replayed(factory,now,mode,state):
    e,s,m,a,r,w=setup(factory,mode)
    try:
        assert e.autonomy.run(now) and e.dispatch_one(now+600)
        assert s.inspect('actions')[0]['state']==state
        assert not e.dispatch_one(now+700) and len(w)==1
        assert not [x for x in s.inspect('conversation_records') if x['role']=='agent']
        if state=='unknown':assert not e.autonomy.run(now+86400)
    finally:a.close()


@pytest.mark.parametrize('changes',[{'visibility':'private'},{'account_id':'999'},{'recipient_id':'123'}])
def test_space_rejects_wrong_route_before_write(factory,now,changes):
    e,s,m,a,r,w=setup(factory)
    try:
        assert e.autonomy.run(now)
        action=Action.from_dict(json.loads(s.inspect('actions')[0]['payload']))
        assert a.send(replace(action,**changes),{}).state=='failed' and not w
    finally:a.close()


def test_space_drafts_and_final_gate_prevent_writes(factory,now):
    e,s,m,a,r,w=setup(factory)
    try:
        r['can_send']=False
        assert e.autonomy.run(now)
        assert not e.dispatch_one(now+600,pre_reviewed_only=True)
        action=Action.from_dict(json.loads(s.inspect('actions')[0]['payload']))
        assert a.send(action,{},before_write=lambda:False).state=='failed' and not w
    finally:a.close()


def test_bili_dynamic_uses_same_public_memory_context(factory,now):
    e,s,m,a,r,w=setup(factory)
    try:
        event=Event('public','bilibili','robot','424242','thread','reply','公开记忆构图练习',now-3600,'public')
        e.ingest(event);s.finish_event(event.event_id,'done')
        e.autonomy.targets=(AutonomyTarget('bilibili','robot',e.autonomy.targets[0].settings),)
        assert e.autonomy.run(now)
        assert '公开记忆构图练习' in json.dumps(m.calls[0][1],ensure_ascii=False)
    finally:a.close()


def test_space_settings_cancel_only_space_and_preserve_private_and_bili(console,now):
    path,s,c=console
    from murasamebot.bootstrap.config import Config
    from murasamebot.bootstrap.runtime import prepare_store
    Config(path).save_local({'platforms':{'qq':{'enabled':True}}})
    prepare_store(Config(path))
    for key,platform,kind in [('space','qq','autonomous_post'),('bili','bilibili','autonomous_post')]:
        assert s.enqueue_action(Action(key,s.agent_id,platform,'456',None,'测试计划',now,now+3600,
            kind=kind,not_before=now,autonomy_version='old'),draft=True,now=now)
    before=c.get('/api/config').json()
    assert c.post('/api/config',json={'qq_space':{'dynamic_enabled':True}}).status_code==200
    data=c.get('/api/platforms/qq').json()
    assert data['space']['settings']['dynamic_enabled']
    after=c.get('/api/config').json()
    assert before['qq_autonomy']==after['qq_autonomy'] and before['autonomy']==after['autonomy']
    assert {r['action_id']:r['state'] for r in s.inspect('actions')}=={'space':'cancelled','bili':'draft'}
    assert c.post('/api/config',json={'qq_space':{'private_enabled':True}}).status_code==400
