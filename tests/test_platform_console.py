from dataclasses import replace
from http.server import ThreadingHTTPServer
from threading import Thread
import json

import httpx
import pytest
import yaml

from murasamebot.bootstrap.config import Config
from murasamebot.bootstrap.runtime import prepare_store
from murasamebot.bootstrap.web import handler_for
from murasamebot.domain.models import Action, ContentRef, Event, Receipt
from murasamebot.application.memory import MemoryService, MemorySettings
from murasamebot.application.task_routing import owner_video_route
from test_architecture_and_config import write_config


@pytest.fixture
def console(tmp_path):
    path=write_config(tmp_path)
    local={'qq':{'robot_id':'456','access_token':'private-api-token','callback_secret':'private-callback-token'},
           'bindings':[{'person_id':'owner','platform':'qq','external_id':'123','purpose':'owner_video_recommendation'},
                       {'person_id':'owner','platform':'bilibili','external_id':'424242','purpose':'owner_video_recommendation'}]}
    Config(path).local_path.write_text(yaml.safe_dump(local),encoding='utf-8')
    store=prepare_store(Config(path))
    server=ThreadingHTTPServer(('127.0.0.1',0),handler_for(path,'test-admin'))
    thread=Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}',headers={'Authorization':'Bearer test-admin'},trust_env=False) as client:
            yield path,store,client
    finally:
        server.shutdown();server.server_close();thread.join(timeout=3)


def seed_task(store,now,*,source='qq',public=False,key='request'):
    peer='123' if source=='qq' else '424242'
    event=Event(key,source,'456' if source=='qq' else 'robot',peer,'room-'+source,
                'private_message','帮我找视频，在B站@我' if public else '找视频发到QQ',now,'private')
    store.enqueue_event(event);store.finish_event(event.event_id,'done')
    route=owner_video_route(store,event,'owner',provider='bilibili',private_platform='qq',public_platform='bilibili',
                           accounts={'qq':'456','bilibili':'robot'}) if source=='qq' else None
    target=route['platform'] if route else 'bilibili'
    memory=store.add_memory('owner','person','owner_video_recommendation','测试视频',event.event_id,now,kind='request')
    task=store.add_task('owner','测试视频',event.event_id,memory,now,now+600,now+3600,
        store.binding('owner',target)['version'],target_platform=target,**({'route':route} if route else {}))
    return event,task


def test_platform_console_filters_tasks_by_source_and_keeps_cross_platform_delivery(console,now):
    path,s,client=console
    private,qq_task=seed_task(s,now)
    public,mention_task=seed_task(s,now+1,public=True,key='mention')
    _,bili_task=seed_task(s,now+2,source='bilibili',key='bili')
    assert s.enqueue_action(Action('qq-result',s.agent_id,'qq','456',None,'私人回复',now,now+300,
        kind='private_reply',recipient_id='123',visibility='private',event_id=private.event_id,
        conversation_id=private.conversation_id),draft=True,now=now)
    assert s.enqueue_action(Action('bili-result',s.agent_id,'bilibili','robot',ContentRef('bilibili','video','10001'),'公开回复',now,now+300,
        kind='recommendation',mentions=('owner',),binding_version=s.binding('owner','bilibili')['version'],memory_epoch=0,
        event_id=public.event_id,task_id=mention_task,task_version=s.task(mention_task)['version']),draft=True,now=now)
    qq=client.get('/api/platforms/qq').json();bili=client.get('/api/platforms/bilibili').json()
    assert {r['task_id'] for r in qq['tasks']}=={qq_task,mention_task}
    assert {r['task_id'] for r in bili['tasks']}=={bili_task}
    assert next(r for r in qq['tasks'] if r['task_id']==mention_task)['route']['mode']=='mention'
    assert {r['action_id'] for r in qq['actions']}=={'qq-result','bili-result'}
    assert {r['action_id'] for r in bili['actions']}=={'bili-result'}
    assert 'private-api-token' not in json.dumps(qq) and 'private-callback-token' not in json.dumps(qq)
    assert client.post('/api/cancel',json={'id':qq_task}).status_code==200
    assert next(r for r in client.get('/api/platforms/qq').json()['tasks'] if r['task_id']==qq_task)['state']=='cancelled'
    assert s.task(bili_task)['state']=='scheduled'


def test_cross_platform_outgoing_record_uses_actual_delivery_platform(console,now):
    _,s,client=console
    event,task=seed_task(s,now,public=True)
    action=Action('public-delivery',s.agent_id,'bilibili','robot',ContentRef('bilibili','video','10001'),'公开的推荐配文',now,now+300,
        kind='recommendation',mentions=('owner',),binding_version=s.binding('owner','bilibili')['version'],memory_epoch=0,
        event_id=event.event_id,task_id=task,task_version=s.task(task)['version'])
    assert s.enqueue_action(action,draft=False,now=now)
    assert s.claim_action(now,20,3,now-3600,1,60,task_window_open=True).action_id==action.action_id
    s.finish_action(action.action_id,Receipt('accepted','mock-public-receipt'),now+1)
    qq=client.get('/api/platforms/qq').json();bili=client.get('/api/platforms/bilibili').json()
    assert all(r['role']=='user' for r in qq['records'])
    assert bili['records'][0]['platform']=='bilibili' and bili['records'][0]['visibility']=='public'
    assert bili['records'][0]['event_id']==event.event_id
    assert 'QQ' not in bili['records'][0]['text'] and event.text!=bili['records'][0]['text']


def test_platform_console_auth_settings_preserve_other_platform_and_secrets(console):
    path,s,client=console
    assert client.get('/api/platforms/qq',headers={'Authorization':''}).status_code==401
    assert client.get('/api/platforms/unknown').status_code==400
    before=Config(path).data
    response=client.post('/api/config',json={'platforms':{'qq':{'enabled':True}},'qq':{'quiet_seconds':45,'max_wait_seconds':180,'max_reply_messages':4}})
    assert response.status_code==200
    after=Config(path).data
    assert after['qq']['quiet_seconds']==45 and after['platforms']['qq']['enabled']
    assert after['bilibili']==before['bilibili'] and after['browsing']==before['browsing']
    assert after['qq']['access_token']=='private-api-token' and after['qq']['callback_secret']=='private-callback-token'
    assert client.post('/api/config',json={'qq':{'access_token':'overwrite'}}).status_code==400
    assert client.post('/api/config',json={'qq':{'quiet_seconds':90,'max_wait_seconds':30}}).status_code==400


def test_qq_only_chat_produces_common_memory_diary_reflection_with_sources(console,now):
    path,s,client=console
    event=Event('qq-chat','qq','456','123','shared-room','private_message','我喜欢摄影。',now,'private')
    s.enqueue_event(event);s.finish_event(event.event_id,'done')
    action=Action('qq-reply',s.agent_id,'qq','456',None,'吾辈记住啦。',now,now+300,
        kind='private_reply',event_id=event.event_id,recipient_id='123',visibility='private',conversation_id=event.conversation_id)
    assert s.enqueue_action(action,draft=False,now=now)
    assert s.claim_action(now,20,3,now-3600,1,60).action_id==action.action_id
    s.finish_action(action.action_id,Receipt('accepted','mock-receipt'),now+1)
    class Model:
        calls=[]
        def decide(self,operation,payload):
            self.calls.append(payload)
            record=next(r for r in payload['records'] if r['role']=='user')
            return {'summary':'主人聊起摄影，吾辈接住了这个话题。',
                'facts':[{'text':'主人喜欢摄影。','sources':[record['record_id']],'quote':'我喜欢摄影','supersedes':[]}],
                'reflection':'又多懂了主人一点。'}
    model=Model()
    memory=MemoryService(s,model,MemorySettings(summary_messages=1,recent_messages=0),timezone='Asia/Hong_Kong')
    assert memory.maintain(now+3)
    assert memory.maintain(now+86400+3)
    assert any(r['role']=='agent' for p in model.calls for r in p['records'])
    feeds=client.get('/api/memory/journals?platform=qq').json()
    assert {'summary','fact'}<={p['kind'] for p in feeds['memory']}
    assert feeds['diary'] and feeds['reflection']
    for product in [*feeds['memory'],*feeds['diary'],*feeds['reflection']]:
        assert product['platforms']==['qq']
        sources=client.post('/api/memory/sources',json={'product_id':product['product_id']}).json()
        assert sources and all(r['platform']=='qq' for r in sources)
    assert not any(client.get('/api/memory/journals?platform=bilibili').json().values())
    assert client.get('/api/memory/journals?platform=untrusted').status_code==400
    client.post('/api/memory/forget-source',json={'id':event.event_id})
    assert not any(client.get('/api/memory/journals?platform=qq').json().values())


def test_memory_platform_filter_precedes_feed_limit_and_agent_boundary(console,now):
    _,s,client=console
    for platform,peer in [('qq','123'),('bilibili','424242')]:
        e=Event(platform,platform,'robot',peer,platform,'chat','来自该平台的交流',now,'private')
        s.enqueue_event(e);s.finish_event(e.event_id,'done')
        records=s.memory_records('owner','conversation:'+platform+':'+platform,'private_dialogue')
        assert s.memory_commit(records,[{'kind':'diary','text':'当天的小记录','sources':[records[0]['record_id']]}],'diary',s.get_setting('memory_epoch',0),now+(100 if platform=='bilibili' else 0))
    assert s.memory_feed('diary',limit=1,platform='qq')[0]['platforms']==['qq']
    assert len(client.get('/api/memory/journals').json()['diary'])==2
