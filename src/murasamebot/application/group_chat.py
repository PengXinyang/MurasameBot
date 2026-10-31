"""Passive scoped conversation; no platform protocol or autonomous loop."""
from murasamebot.domain.models import Action,stable_id
from murasamebot.domain.topics import local_topic,combined_topic,blocked

DEFAULTS={'enabled':False,'group_ids':[],'context_messages':30,'context_minutes':30,
          'cooldown_seconds':30,'daily_reply_limit':100,'max_characters':400}


def validate(settings):
    if not isinstance(settings,dict) or set(settings)!=set(DEFAULTS) or type(settings['enabled']) is not bool:
        raise ValueError('群聊设置需要合法字段及布尔开关')
    ids=settings['group_ids']
    if not isinstance(ids,list) or len(ids)>50 or len(set(map(str,ids)))!=len(ids) or any(not isinstance(v,str) or not v.isascii() or not v.isdigit() or not 1<=len(v)<=20 for v in ids):
        raise ValueError('群号需为不重复的数字字符串列表，最多50项')
    if settings['enabled'] and not ids:raise ValueError('开启群聊前请指定群号')
    for k,low,high in [('context_messages',1,100),('context_minutes',1,1440),('cooldown_seconds',1,3600),('daily_reply_limit',1,1000),('max_characters',1,1000)]:
        if type(settings[k]) is not int or not low<=settings[k]<=high:raise ValueError('群聊参数超出范围：'+k)


class GroupChat:
    def __init__(self,engine,settings,inbox,access):
        self.engine,self.settings,self.inbox,self.access=engine,settings,inbox,access

    def handle(self,event,person,now):
        e,s=self.engine,self.settings
        if not s['enabled'] or event.thread_root not in s['group_ids'] or e.paused(event.platform):return
        epoch=e.store.get_setting('memory_epoch',0)
        messages=self.inbox.context(event,s,now)
        if not any(m.event_id==event.event_id for m in messages):return
        selected,characters=[],0
        for message in reversed(messages):
            if characters+len(message.text)<=12000:
                selected.append(message);characters+=len(message.text)
        messages=list(reversed(selected))
        self.access.check_group(event.thread_root,event.actor_id)
        owner=person==e.settings.owner_person_id
        if blocked(local_topic(event.text),owner):return
        result,options=e.message_decision('group_chat',{'persona':e.persona.prompt(False),
            'owner_addressed':owner,'target_user_id':event.actor_id,'target_name':event.actor_name,
            'message':event.text,'quoted_message_id':event.reply_to,
            'context':[{'user_id':m.actor_id,'name':m.actor_name,'text':m.text,'message_id':str(m.sequence),
                'reply_to':m.reply_to,'time':m.occurred_at,'role':'agent' if m.actor_id==event.account_id else 'user'} for m in messages],
            'quoted_message':self.access.group_quote(event.thread_root,event.reply_to) if event.reply_to else None,
            'memory_epoch':epoch,'max_characters':s['max_characters']},event.platform,event.account_id,'group',now)
        if result.get('decision')=='skip':return
        if result.get('decision')!='reply' or blocked(combined_topic(local_topic(result.get('text','')),result.get('topic_class')),owner):return
        text,emojis=e.message_text(result,options,s['max_characters'],multilingual=True)
        action=Action(stable_id(e.settings.agent_id,'group',event.event_id),e.settings.agent_id,event.platform,event.account_id,
            None,text,now,now+min(300,s['context_minutes']*60),kind='group_reply',event_id=event.event_id,
            conversation_id=event.conversation_id,recipient_id=event.actor_id,visibility='group',reply_to=str(event.sequence),
            source_sequence=event.sequence,dedup_key=stable_id(event.event_id,s),memory_epoch=result['_memory_epoch'],
            evidence_level='conversation',emojis=emojis,topic_class=result.get('topic_class','unknown'),decision_reason='群内被动接话')
        if not self.inbox.guard(action,s,now):e.enqueue_generated(action,result,now)
