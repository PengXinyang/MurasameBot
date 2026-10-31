"""Autonomous windows use the same engine and fake OneBot as passive chat."""
import json
from copy import deepcopy
import pytest
from test_qq_groups import chat,raw
from murasamebot.application.group_chat import GroupChat,validate,DEFAULTS
from murasamebot.adapters.persistence.group_inbox import GroupInbox


def enable(chat):
    e,s,a,inbox,state,calls,now=chat
    a.group_settings['autonomous_enabled']=True
    e.event_handlers['group_window']=GroupChat(e,a.group_settings,inbox,a).handle
    def model(op,p):
        calls.append((op,deepcopy(p)))
        return {'decision':'reply','target_user_id':'123','target_message_id':'10',
            'text':'你刚才提到的水彩，可以从简单配色开始。','topic_class':'casual','can_send':True,'review_reason':'合成通过'}
    e.model.decide=model
    a.parse(raw(10,peer=123,at=None,text='想试试水彩。'),now)
    a.parse(raw(11,peer=321,at=None,text='我在画素描。'),now+1)
    return e,s,a,inbox,state,calls,now


def test_window_selects_earlier_speaker_and_preserves_authorship(chat):
    e,s,a,i,state,calls,now=enable(chat)
    assert not i.flush(a.group_settings,now+8)
    assert i.flush(a.group_settings,now+9)==1
    assert not GroupInbox(s).flush(a.group_settings,now+10)
    assert e.process_one_event(now+9)
    p=calls[0][1]
    assert p['autonomous'] and p['target_user_id']==''
    assert [(m['user_id'],m['message_id']) for m in p['context']]==[('123','10'),('321','11')]
    state['clock']=now+9
    assert e.dispatch_one(now+9)
    assert state['writes'][0]['message'][:2]==[{'type':'reply','data':{'id':'10'}},{'type':'at','data':{'qq':'123'}}]
    assert not i.flush(a.group_settings,now+1000) # no new message, no self-started loop


@pytest.mark.parametrize('change',[{'target_user_id':'999'},{'target_message_id':'999'},
    {'target_user_id':'321'}, {'target_user_id':'456'},{'target_user_id':None}])
def test_model_cannot_choose_outside_window_or_mismatch_author(chat,change):
    e,s,a,i,state,calls,now=enable(chat)
    decide=e.model.decide
    e.model.decide=lambda op,p:{**decide(op,p),**change}
    assert i.flush(a.group_settings,now+9)
    e.process_one_event(now+9)
    assert not s.inspect('actions') and not state['writes']


def test_skip_is_durable_and_decision_interval_applies(chat):
    e,s,a,i,state,calls,now=enable(chat)
    e.model.decide=lambda op,p:{'decision':'skip'}
    assert i.flush(a.group_settings,now+9)
    e.process_one_event(now+9)
    assert not s.inspect('actions')
    a.parse(raw(12,at=None),now+10)
    assert not GroupInbox(s).flush(a.group_settings,now+30)
    assert GroupInbox(s).flush(a.group_settings,now+69)


def test_continuous_messages_have_maximum_wait_and_one_inflight_window(chat):
    e,s,a,i,state,calls,now=enable(chat)
    for mid in range(12,41):a.parse(raw(mid,at=None),now+mid-10)
    assert i.flush(a.group_settings,now+30)==1
    a.parse(raw(42,at=None),now+31)
    assert not i.flush(a.group_settings,now+100) # first turn not processed yet
    e.process_one_event(now+100) # stale window silently discarded
    assert not calls
    assert i.flush(a.group_settings,now+100)==1


@pytest.mark.parametrize('when',['generation','queued','final_check'])
def test_new_message_invalidates_old_reply(chat,when):
    e,s,a,i,state,calls,now=enable(chat)
    decide=e.model.decide
    def new():a.parse(raw(12,at=None,text='刚刚的问题已经解决了。'),now+10)
    if when=='generation':
        def model(op,p):
            result=decide(op,p);new();return result
        e.model.decide=model
    assert i.flush(a.group_settings,now+9)
    e.process_one_event(now+9)
    if when=='queued':new()
    if when=='final_check':
        check=a.check_group
        def checked(group,peer):check(group,peer);new()
        a.check_group=checked
    state['clock']=now+11
    e.dispatch_one(now+11)
    assert not state['writes']


def test_decision_budget_counts_skips_and_is_per_group(chat):
    e,s,a,i,state,calls,now=enable(chat)
    a.group_settings['daily_decision_limit']=1
    e.model.decide=lambda op,p:{'decision':'skip'}
    assert i.flush(a.group_settings,now+9)
    e.process_one_event(now+9)
    a.parse(raw(12,at=None),now+80)
    a.parse(raw(13,group=790,at=None),now+80)
    assert i.flush(a.group_settings,now+89)==1
    pending=[json.loads(r['payload']) for r in s.inspect('events') if r['state']=='pending']
    assert len(pending)==1 and pending[0]['thread_root']=='790'


def test_mode_change_and_memory_clear_prevent_restart_replay(chat):
    e,s,a,i,state,calls,now=enable(chat)
    assert i.flush(a.group_settings,now+9)
    e.process_one_event(now+9)
    a.group_settings['autonomous_enabled']=False
    state['clock']=now+10;e.dispatch_one(now+10)
    assert not state['writes']
    s.set_setting('memory_epoch',s.get_setting('memory_epoch',0)+1)
    a.group_settings['autonomous_enabled']=True
    assert not GroupInbox(s).flush(a.group_settings,now+100)


def test_window_size_restricts_eligible_targets(chat):
    e,s,a,i,state,calls,now=enable(chat)
    a.group_settings['context_messages']=1
    assert i.flush(a.group_settings,now+9)
    e.process_one_event(now+9)
    assert [m['message_id'] for m in calls[0][1]['context']]==['11']
    assert not s.inspect('actions')


def test_reset_does_not_collide_with_old_window_ids_or_reset_budget(chat):
    e,s,a,i,state,calls,now=enable(chat)
    e.model.decide=lambda op,p:{'decision':'skip'}
    assert i.flush(a.group_settings,now+9)
    e.process_one_event(now+9)
    i.reset()
    a.parse(raw(12,at=None),now+20)
    assert i.flush(a.group_settings,now+29)
    assert e.process_one_event(now+29)
    assert len([r for r in s.inspect('events') if json.loads(r['payload'])['kind']=='group_window'])==2
    i.reset();a.group_settings['daily_decision_limit']=2
    a.parse(raw(13,at=None),now+40)
    assert not i.flush(a.group_settings,now+49)


def test_duplicate_old_message_cannot_dirty_new_window(chat):
    e,s,a,i,state,calls,now=enable(chat)
    e.model.decide=lambda op,p:{'decision':'skip'}
    assert i.flush(a.group_settings,now+9)
    e.process_one_event(now+9)
    # Cache has expired, but durable dedup still applies across process recreation.
    a.parse(raw(10,peer=123,at=None,text='想试试水彩。'),now+2000)
    assert not GroupInbox(s).flush(a.group_settings,now+2010)


@pytest.mark.parametrize('change',[{'autonomous_enabled':'true'},{'quiet_seconds':0},
    {'quiet_seconds':40,'max_wait_seconds':30},{'daily_decision_limit':True}])
def test_invalid_window_config(change):
    with pytest.raises(ValueError):validate({**deepcopy(DEFAULTS),**change})
