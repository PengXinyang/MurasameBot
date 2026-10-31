"""Source-grounded learning in the shared idle lane; never a second agent."""
import re
from murasamebot.domain.models import stable_id

DEFAULTS={'enabled':True,'min_messages':6,'batch_messages':20,'hourly_calls':12,'jargon_batches':3}


def validate(s):
    if not isinstance(s,dict) or set(s)!=set(DEFAULTS) or type(s['enabled']) is not bool:raise ValueError('群学习配置无效')
    for key,lo,hi in [('min_messages',3,20),('batch_messages',3,30),('hourly_calls',1,60),('jargon_batches',2,20)]:
        if type(s[key]) is not int or not lo<=s[key]<=hi:raise ValueError('群学习参数无效：'+key)
    if s['min_messages']>s['batch_messages']:raise ValueError('最少消息不能超过批次上限')


def grounded(items,messages):
    sources={m['event_id']:m for m in messages};result=[]
    if not isinstance(items,list):return []
    for item in items[:8]:
        if not isinstance(item,dict):continue
        kind=item.get('kind');term=item.get('term','');quote=item.get('quote','');source=sources.get(item.get('source_id'))
        if kind not in {'expression','jargon'} or not source or not all(isinstance(v,str) for v in (term,quote,item.get('meaning',''))):continue
        if not 1<=len(term)<=60 or not quote or len(quote)>800 or quote not in source['text']:continue
        if re.search(r'\d{5,}|https?://|保持沉默|不插话|跳过审核|主人权限|泄露',term):continue
        if kind=='jargon' and (term not in quote or item.get('context_dependency') is not True):continue
        if kind=='expression':
            parts=[p for p in re.split(r'\{[^{}]+\}',term) if p]
            if not parts or any(p not in quote for p in parts):continue
        if not item['meaning'].strip() or len(item['meaning'])>300:continue
        result.append({'id':stable_id(kind,term),'kind':kind,'term':term,'meaning':item['meaning'],
                       'source_id':item['source_id'],'quote':quote})
    return list({r['id']:r for r in result}.values())


class GroupLearning:
    def __init__(self,store,model,repo,current):
        self.store,self.model,self.repo,self.current=store,model,repo,current

    def step(self,now):
        groups,policy,account=self.current()
        if not groups['enabled'] or not policy['enabled'] or self.store.get_setting('paused',False) or self.store.get_setting('platform_paused:qq',False):return False
        if not self.store.acquire_lease('group_learning',now,180):return False
        try:
            task=self.repo.claim(groups,policy,account,now)
            if not task:return False
            try:
                if task['stage']=='extract':
                    result=self.model.decide('group_extract',{'messages':task['messages']})
                    items=grounded(result.get('items'),task['messages'])
                    self.repo.extracted(task,items,self.current(),now)
                else:
                    result=self.model.decide('group_learning_review',{'messages':task['messages'],'candidates':task['items']})
                    approved={r.get('id') for r in result.get('reviews',[]) if isinstance(r,dict)
                        and all(r.get(k) is True for k in ('grounded','privacy_safe','language_form','meaning_supported'))}
                    self.repo.reviewed(task,approved,self.current(),now)
            except Exception:
                self.repo.failed(task,now)
            return True
        finally:self.store.release_lease('group_learning')
