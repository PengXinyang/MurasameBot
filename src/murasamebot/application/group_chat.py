"""Scoped group decisions, driven by the existing coordinator."""
from murasamebot.domain.models import Action,stable_id
from murasamebot.domain.topics import local_topic,combined_topic,blocked

DEFAULTS={'enabled':False,'group_ids':[],'context_messages':30,'context_minutes':30,
          'cooldown_seconds':30,'daily_reply_limit':100,'max_characters':400,'autonomous_enabled':False,'quiet_seconds':8,'max_wait_seconds':30,
          'decision_interval_seconds':60,'daily_decision_limit':100}


def validate(settings):
    if not isinstance(settings,dict) or set(settings)!=set(DEFAULTS) or type(settings['enabled']) is not bool or type(settings['autonomous_enabled']) is not bool:
        raise ValueError('群聊设置需要合法字段及布尔开关')
    ids=settings['group_ids']
    if not isinstance(ids,list) or len(ids)>50 or len(set(map(str,ids)))!=len(ids) or any(not isinstance(v,str) or not v.isascii() or not v.isdigit() or not 1<=len(v)<=20 for v in ids):
        raise ValueError('群号需为不重复的数字字符串列表，最多50项')
    if settings['enabled'] and not ids:raise ValueError('开启群聊前请指定群号')
    for k,low,high in [('context_messages',1,100),('context_minutes',1,1440),('cooldown_seconds',1,3600),('daily_reply_limit',1,1000),('max_characters',1,1000),('quiet_seconds',1,120),('max_wait_seconds',1,600),('decision_interval_seconds',1,3600),('daily_decision_limit',1,1000)]:
        if type(settings[k]) is not int or not low<=settings[k]<=high:raise ValueError('群聊参数超出范围：'+k)
    if settings['max_wait_seconds']<settings['quiet_seconds']:raise ValueError('最长收集等待不能短于静默窗口')


class GroupChat:
    def __init__(self,engine,settings,inbox,access):
        self.engine,self.settings,self.inbox,self.access=engine,settings,inbox,access

    def handle(self,event,person,now):
        e,s=self.engine,self.settings
        if not s['enabled'] or event.thread_root not in s['group_ids'] or e.paused(event.platform):return
        autonomous=event.kind=='group_window'
        if not autonomous and s['autonomous_enabled']:return
        if autonomous and not self.inbox.valid_window(event.event_id,s):return
        epoch=e.store.get_setting('memory_epoch',0)
        messages=self.inbox.context(event,s,now)
        if not autonomous and not any(m.event_id==event.event_id for m in messages):return
        selected,characters=[],0
        for message in reversed(messages):
            if characters+len(message.text)<=12000:
                selected.append(message);characters+=len(message.text)
        messages=list(reversed(selected))
        if autonomous and not any(m.kind=='group_message' for m in messages):return
        self.access.check_group(event.thread_root,event.actor_id)
        owner=person==e.settings.owner_person_id
        if not autonomous and blocked(local_topic(event.text),owner):return
        result,options=e.message_decision('group_chat',{'persona':e.persona.prompt(False),
            'autonomous':autonomous,'owner_addressed':owner if not autonomous else False,'target_user_id':event.actor_id if not autonomous else '','target_name':event.actor_name if not autonomous else '',
            'message':event.text if not autonomous else '', 'quoted_message_id':event.reply_to if not autonomous else '',
            'context':[{'user_id':m.actor_id,'name':m.actor_name,'text':m.text,'message_id':str(m.sequence),
                'reply_to':m.reply_to,'time':m.occurred_at,'is_owner':e.store.resolve_person(m.platform,m.actor_id)==e.settings.owner_person_id,'role':'agent' if m.actor_id==event.account_id else 'user'} for m in messages],
            'quoted_message':self.access.group_quote(event.thread_root,event.reply_to) if event.reply_to and not autonomous else None,
            'memory_epoch':epoch,'max_characters':s['max_characters']},event.platform,event.account_id,'group',now)
        if result.get('decision')=='skip':return
        if result.get('decision')!='reply':return
        if not autonomous and blocked(combined_topic(local_topic(result.get('text','')),result.get('topic_class')),owner):return
        window_id=event.event_id if autonomous else ''
        if autonomous:
            targets=[m for m in messages if m.actor_id!=event.account_id and m.kind=='group_message'
                and str(m.sequence)==str(result.get('target_message_id','')) and m.actor_id==str(result.get('target_user_id',''))]
            if len(targets)!=1 or not self.inbox.valid_window(window_id,s):return
            event=targets[0]
            owner=e.store.resolve_person(event.platform,event.actor_id)==e.settings.owner_person_id
            if blocked(local_topic(event.text),owner) or blocked(combined_topic(local_topic(result.get('text','')),result.get('topic_class')),owner):return
            self.access.check_group(event.thread_root,event.actor_id)
            if e.store.enqueue_event(event,baseline=True):e.store.finish_event(event.event_id,'done')
        text,emojis=e.message_text(result,options,s['max_characters'],multilingual=True)
        action=Action(stable_id(e.settings.agent_id,'group',window_id or event.event_id),e.settings.agent_id,event.platform,event.account_id,
            None,text,now,now+min(300,s['context_minutes']*60),kind='group_reply',event_id=event.event_id,
            conversation_id=event.conversation_id,recipient_id=event.actor_id,visibility='group',reply_to=str(event.sequence),
            source_sequence=event.sequence,dedup_key=stable_id(event.event_id,s),memory_epoch=result['_memory_epoch'],
            evidence_level='conversation',emojis=emojis,topic_class=result.get('topic_class','unknown'),decision_reason='群内自主接话' if autonomous else '群内被动接话')
        if autonomous:self.inbox.plan(action,window_id)
        if not self.inbox.guard(action,s,now):e.enqueue_generated(action,result,now)
