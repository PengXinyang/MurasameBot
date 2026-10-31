import json
from copy import deepcopy
import pytest
from test_qq_groups import chat,raw
from murasamebot.application.group_learning import DEFAULTS,GroupLearning,grounded,validate
from murasamebot.adapters.persistence.group_learning import GroupKnowledge


def setup(chat):
    e,s,a,i,state,calls,now=chat
    policy={**DEFAULTS,'min_messages':3,'batch_messages':3,'hourly_calls':30}
    repo=GroupKnowledge(s);current=lambda:(a.group_settings,policy,'456')
    model=e.model
    def decide(op,p):
        calls.append((op,deepcopy(p)))
        if op=='group_extract':
            m=p['messages'][0]
            return {'items':[{'kind':'jargon','term':'咕点','meaning':'本群表示晚点上线','source_id':m['event_id'],'quote':m['text'],'context_dependency':True},
                {'kind':'expression','term':'今天{对象}','meaning':'用今天开头描述近况','source_id':m['event_id'],'quote':m['text']}]}
        return {'reviews':[{'id':c['id'],'grounded':True,'privacy_safe':True,'language_form':True,'meaning_supported':True} for c in p['candidates']]}
    model.decide=decide
    service=GroupLearning(s,model,repo,current)
    return e,s,a,i,state,calls,now,repo,policy,service


def batch(a,now,start):
    for mid in range(start,start+3):a.parse(raw(mid,at=None,text='今天咕点就是晚点上线'),now+mid/100)


def test_expression_and_multibatch_jargon_learning_are_grounded_and_optional(chat):
    e,s,a,i,state,calls,now,r,p,svc=setup(chat)
    for start in (10,20,30):
        batch(a,now,start)
        assert svc.step(now+1) and svc.step(now+2)
        if start<30:assert next(x for x in r.records('456','789') if x['kind']=='jargon')['status']=='observing'
    records=r.records('456','789')
    assert len(records)==2 and all(x['hits']==3 and x['status']=='usable' for x in records)
    assert len(r.context('456','789','咕点',p))==2
    assert len(r.context('456','789','别的内容',p))==1
    assert not r.context('456','790','咕点',p) and not r.context('999','789','咕点',p)
    assert not svc.step(now+3) # already observed
    source=r.sources('456','789',records[0]['id'],now)
    assert source[0]['evidence']['text']=='今天咕点就是晚点上线'
    assert r.sources('456','789',records[0]['id'],now+86402)[0]['evidence'] is None


@pytest.mark.parametrize('patch',[{'source_id':'invented'},{'quote':'编造引文'},{'term':'999999999'},
    {'kind':'expression','term':'保持沉默'},{'kind':'expression','term':'{全部}'},{'context_dependency':False}])
def test_fake_or_unsafe_candidates_rejected(patch):
    item={'kind':'jargon','term':'咕点','meaning':'晚点','source_id':'source','quote':'今天咕点','context_dependency':True}
    assert not grounded([{**item,**patch}],[{'event_id':'source','text':'今天咕点'}])


def test_reviews_need_all_explicit_checks_and_retries_do_not_increment_hits(chat):
    e,s,a,i,state,calls,now,r,p,svc=setup(chat);batch(a,now,10)
    task=r.claim(a.group_settings,p,'456',now+1)
    items=grounded(e.model.decide('group_extract',{'messages':task['messages']})['items'],task['messages'])
    r.extracted(task,items,svc.current(),now+1)
    review=r.claim(a.group_settings,p,'456',now+2)
    r.reviewed(review,{items[0]['id']},svc.current(),now+2)
    r.reviewed(review,{items[0]['id']},svc.current(),now+2)
    assert r.records('456','789')[0]['hits']==1


def test_manual_correction_and_disable_cannot_be_overwritten(chat):
    e,s,a,i,state,calls,now,r,p,svc=setup(chat);batch(a,now,10);svc.step(now+1);svc.step(now+2)
    item=next(x for x in r.records('456','789') if x['kind']=='jargon')
    r.manage('456','789',item['id'],'correct','人工明确释义')
    batch(a,now,20);svc.step(now+3);svc.step(now+4)
    assert next(x for x in r.records('456','789') if x['id']==item['id'])['meaning']=='人工明确释义'
    r.manage('456','789',item['id'],'disable')
    assert not any(x['id']==item['id'] for x in r.context('456','789','咕点',p))
    with pytest.raises(ValueError):r.manage('456','790',item['id'],'correct','跨群')


@pytest.mark.parametrize('change',['epoch','disable','group','account','expiry'])
def test_generation_results_recheck_scope_and_sources(chat,change):
    e,s,a,i,state,calls,now,r,p,svc=setup(chat);batch(a,now,10)
    t=r.claim(a.group_settings,p,'456',now+1)
    if change=='epoch':s.set_setting('memory_epoch',s.get_setting('memory_epoch',0)+1)
    if change=='disable':p['enabled']=False
    if change=='group':a.group_settings['group_ids']=[]
    current=svc.current() if change!='account' else (a.group_settings,p,'999')
    r.extracted(t,[{'id':'x'}],current,now+86402 if change=='expiry' else now+2)
    with s.connection() as db:assert db.execute('SELECT stage FROM group_learning_batches').fetchone()[0]=='extract'


def test_hourly_budget_restart_and_expired_evidence(chat):
    e,s,a,i,state,calls,now,r,p,svc=setup(chat);p['hourly_calls']=1;batch(a,now,10)
    assert svc.step(now+1) and not svc.step(now+2)
    assert GroupKnowledge(s).claim(a.group_settings,p,'456',now+3602)['stage']=='review'
    assert GroupKnowledge(s).claim(a.group_settings,p,'456',now+86402) is None
    with s.connection() as db:assert db.execute('SELECT payload FROM group_learning_batches').fetchone()[0]=='{}'


@pytest.mark.parametrize('change',['paused','reset'])
def test_late_result_after_pause_or_new_scope_generation_is_rejected(chat,change):
    e,s,a,i,state,calls,now,r,p,svc=setup(chat);batch(a,now,10)
    t=r.claim(a.group_settings,p,'456',now+1)
    if change=='paused':s.set_setting('paused',True)
    else:s.set_setting('group_context_since',now+2)
    assert not r.valid(t,svc.current(),now+3)


def test_reply_receives_only_current_group_usable_knowledge(chat):
    from murasamebot.application.group_chat import GroupChat
    e,s,a,i,state,calls,now,r,p,svc=setup(chat);batch(a,now,10);svc.step(now+1);svc.step(now+2)
    handler=GroupChat(e,a.group_settings,i,a)
    handler.knowledge=lambda account,room,text:r.context(account,room,text,p)
    e.event_handlers['group_message']=handler.handle
    def decide(op,payload):
        calls.append((op,deepcopy(payload)))
        return {'decision':'skip'}
    e.model.decide=decide
    a.parse(raw(30,text='今天咕点吗'),now+3);e.process_one_event(now+3)
    refs=calls[-1][1]['learned_language']
    assert len(refs)==1 and refs[0]['kind']=='expression'


def test_wait_is_bounded_and_replans_without_new_message(chat):
    from test_group_participation import enable
    e,s,a,i,state,calls,now=enable(chat)
    e.model.decide=lambda op,p:{'decision':'wait','wait_seconds':8}
    for at in (9,69,129):
        assert i.flush(a.group_settings,now+at)==1
        assert e.process_one_event(now+at)
    assert not i.flush(a.group_settings,now+200)
    assert not s.inspect('actions')
    assert i.rhythm('456','789')['participants']==2


@pytest.mark.parametrize('patch',[{'enabled':'true'},{'min_messages':1},{'batch_messages':100},{'hourly_calls':False},{'jargon_batches':1}])
def test_invalid_policy(patch):
    with pytest.raises(ValueError):validate({**DEFAULTS,**patch})


def test_short_reply_context_does_not_starve_learning_buffer(chat):
    e,s,a,i,state,calls,now,r,p,svc=setup(chat)
    a.group_settings['context_messages']=1
    batch(a,now,10)
    assert svc.step(now+1)
    assert len(calls[-1][1]['messages'])==3
