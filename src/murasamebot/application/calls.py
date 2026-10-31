"""Voice turns use the same persona, memory, model review and outbox."""
from murasamebot.domain.models import Action,ContentRef,Document,ImageRef,stable_id

DEFAULTS={'enabled':False,'video_enabled':False,'capture_device':'','playback_device':'',
    'asr_url':'','asr_key':'','asr_model':'','tts_url':'','tts_key':'','ref_audio_path':'',
    'prompt_text':'','prompt_lang':'zh','text_lang':'zh','avatar_image':'',
    'silence_ms':800,'vad_threshold':500,'max_utterance_seconds':12,'session_minutes':30}


def validate(s):
    from urllib.parse import urlparse
    if not isinstance(s,dict) or set(s)!=set(DEFAULTS):raise ValueError('通话配置字段无效')
    for k in ('enabled','video_enabled'):
        if type(s[k]) is not bool:raise ValueError('通话开关需为布尔值')
    for k,lo,hi in [('silence_ms',300,3000),('vad_threshold',50,10000),('max_utterance_seconds',2,30),('session_minutes',1,60)]:
        if type(s[k]) is not int or not lo<=s[k]<=hi:raise ValueError('通话参数超出范围：'+k)
    for k,v in DEFAULTS.items():
        if isinstance(v,str) and (not isinstance(s[k],str) or len(s[k])>2000):raise ValueError('通话字段无效：'+k)
    for k in ('asr_url','tts_url'):
        if s[k]:
            u=urlparse(s[k])
            if u.scheme not in {'http','https'} or not u.hostname or u.username or u.password or u.query or u.fragment:raise ValueError('语音接口地址必须为无内嵌凭据的HTTP(S)地址')


class CallConversation:
    def __init__(self,engine,sessions,settings,access,vision=None):
        self.engine,self.sessions,self.settings,self.access,self.vision=engine,sessions,settings,access,vision

    def handle(self,event,person,now):
        try:return self.handle_current(event,person,now)
        except Exception:
            session=self.sessions.for_event(event.event_id)
            if not session or not self.sessions.active(session,self.settings,now) or session['revision']!=session['input_revision']:return
            raise

    def handle_current(self,event,person,now):
        e=self.engine
        session=self.sessions.for_event(event.event_id)
        if not session or session['revision']!=session['input_revision'] or person!=e.settings.owner_person_id or not self.sessions.active(session,self.settings,now):return
        if not self.access.allows_private_reply(event.account_id,event.actor_id):return
        if not self.access.call_available(event.event_id):return
        context=e.memory_service.context(event,person,True,now)
        visual=None
        if session['mode']=='video' and self.vision:
            doc=self.vision.enrich(Document(ContentRef(event.platform,'call_frame',event.event_id),
                '本次通话窗口快照',event.text,'text',images=(ImageRef(event.event_id,1),)),now)
            visual=doc.visual.__dict__ if doc.visual else None
        result,options=e.message_decision('call_chat',{'persona':e.persona.prompt(True,private=True),
            'message':event.text,**context,'visual':visual,'max_characters':160},event.platform,event.account_id,'private',now)
        if result.get('decision')!='reply':return
        text,_=e.message_text(result,(),160,multilingual=True)
        action=Action(stable_id(e.settings.agent_id,'call',event.event_id),e.settings.agent_id,event.platform,event.account_id,
            None,text,now,now+90,kind='call_reply',event_id=event.event_id,conversation_id=event.conversation_id,
            recipient_id=event.actor_id,visibility='private',source_sequence=session['input_revision'],
            memory_epoch=result['_memory_epoch'],evidence_level='conversation',decision_reason='主人QQ通话回复')
        if not self.sessions.guard(action,self.settings,now):e.enqueue_generated(action,result,now)
