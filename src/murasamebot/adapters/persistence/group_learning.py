"""Shared SQLite, partitioned knowledge and expiring source batches."""
import json
import time
from murasamebot.domain.models import stable_id


class GroupKnowledge:
    def __init__(self,store):
        self.store=store
        with store.connection() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS group_learning_batches(
                agent TEXT,id TEXT,account TEXT,room TEXT,created REAL,epoch INTEGER,version TEXT,
                stage TEXT,payload TEXT,attempts INTEGER DEFAULT 0,retry_at REAL DEFAULT 0,
                PRIMARY KEY(agent,id))''')
            db.execute('CREATE TABLE IF NOT EXISTS group_learning_seen(agent TEXT,id TEXT,PRIMARY KEY(agent,id))')
            db.execute('''CREATE TABLE IF NOT EXISTS group_knowledge(
                agent TEXT,account TEXT,room TEXT,id TEXT,kind TEXT,term TEXT,meaning TEXT,status TEXT,
                hits INTEGER,manual INTEGER,epoch INTEGER,PRIMARY KEY(agent,account,room,id))''')
            db.execute('''CREATE TABLE IF NOT EXISTS group_knowledge_sources(
                agent TEXT,account TEXT,room TEXT,item TEXT,batch TEXT,source TEXT,quote_hash TEXT,
                PRIMARY KEY(agent,account,room,item,batch))''')
            db.execute('CREATE TABLE IF NOT EXISTS group_learning_calls(agent TEXT,account TEXT,room TEXT,at REAL)')
            db.execute("UPDATE group_learning_batches SET payload='{}',stage='expired' WHERE agent=? AND created<?",(store.agent_id,time.time()-86400))

    def claim(self,groups,policy,account,now):
        if not groups['enabled'] or not policy['enabled']:return None
        epoch=self.store.get_setting('memory_epoch',0);version=stable_id(groups,policy)
        with self.store.transaction() as db:
            db.execute("UPDATE group_learning_batches SET payload='{}',stage='cancelled' WHERE agent=? AND stage IN ('extract','review') AND (epoch!=? OR created<?)",(self.store.agent_id,epoch,self.store.get_setting('group_context_since',0)))
            db.execute("UPDATE group_learning_batches SET payload='{}',stage='expired' WHERE agent=? AND created<? AND stage!='expired'",(self.store.agent_id,now-86400))
            # Hard evidence cap per partition, including completed batches.
            db.execute("UPDATE group_learning_batches SET payload='{}',stage='expired' WHERE agent=? AND id IN (SELECT id FROM (SELECT id,ROW_NUMBER() OVER(PARTITION BY account,room ORDER BY created DESC,rowid DESC) AS n FROM group_learning_batches WHERE agent=?) WHERE n>100)",(self.store.agent_id,self.store.agent_id))
            for room in groups['group_ids']:
                args=(self.store.agent_id,account,room)
                if db.execute('SELECT COUNT(*) FROM group_learning_calls WHERE agent=? AND account=? AND room=? AND at>?',(*args,now-3600)).fetchone()[0]>=policy['hourly_calls']:continue
                row=db.execute("SELECT * FROM group_learning_batches WHERE agent=? AND account=? AND room=? AND stage IN ('extract','review') AND retry_at<=? AND created>=? AND epoch=? AND version=? ORDER BY created LIMIT 1",(*args,now,now-86400,epoch,version)).fetchone()
                if row:
                    task={**dict(row),**json.loads(row['payload'])}
                else:
                    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='group_inbox'").fetchone():continue
                    rows=db.execute('''SELECT g.payload FROM group_inbox g WHERE g.agent=? AND g.account=? AND g.room=? AND g.received>=?
                        AND NOT EXISTS(SELECT 1 FROM group_learning_seen s WHERE s.agent=g.agent AND s.id=g.id)
                        AND NOT EXISTS(SELECT 1 FROM memory_suppressed s WHERE s.agent_id=g.agent AND s.event_id=g.id)
                        ORDER BY g.received,g.rowid LIMIT ?''',(*args,now-min(86400,groups['context_minutes']*60),policy['batch_messages'])).fetchall()
                    messages=[];size=0
                    for r in rows:
                        e=json.loads(r[0]);text=e['text'][:800]
                        if e['actor_id']==account or e['kind']!='group_message':continue
                        if size+len(text)>16000:break
                        messages.append({'event_id':e['event_id'],'user_id':e['actor_id'],'text':text,'time':e['occurred_at']});size+=len(text)
                    if len(messages)<policy['min_messages']:continue
                    key=stable_id(self.store.agent_id,account,room,epoch,[m['event_id'] for m in messages])
                    task={'agent':self.store.agent_id,'id':key,'account':account,'room':room,'epoch':epoch,'version':version,'created':now,'stage':'extract','messages':messages,'attempts':0}
                    db.execute("INSERT OR IGNORE INTO group_learning_batches(agent,id,account,room,created,epoch,version,stage,payload) VALUES(?,?,?,?,?,?,?,?,?)",(self.store.agent_id,key,account,room,now,epoch,version,'extract',json.dumps({'messages':messages},ensure_ascii=False)))
                    for m in messages:db.execute('INSERT OR IGNORE INTO group_learning_seen VALUES(?,?)',(self.store.agent_id,m['event_id']))
                db.execute('INSERT INTO group_learning_calls VALUES(?,?,?,?)',(*args,now))
                return task

    def valid(self,task,current,now):
        now=max(now,time.time())
        groups,policy,account=current
        return (groups['enabled'] and policy['enabled'] and task['account']==account and task['room'] in groups['group_ids']
                and task['version']==stable_id(groups,policy) and task['epoch']==self.store.get_setting('memory_epoch',0)
                and task['created']>=max(now-86400,self.store.get_setting('group_context_since',0))
                and not self.store.get_setting('paused',False) and not self.store.get_setting('platform_paused:qq',False))

    def extracted(self,task,items,current,now):
        if not self.valid(task,current,now):return
        with self.store.connection() as db:
            db.execute("UPDATE group_learning_batches SET stage=?,payload=?,attempts=0 WHERE agent=? AND id=? AND stage='extract'",
                ('review' if items else 'done',json.dumps({'messages':task['messages'],'items':items},ensure_ascii=False),self.store.agent_id,task['id']))

    def reviewed(self,task,approved,current,now):
        if not self.valid(task,current,now):return
        policy=current[1]
        with self.store.transaction() as db:
            if not db.execute("UPDATE group_learning_batches SET stage='done' WHERE agent=? AND id=? AND stage='review'",(self.store.agent_id,task['id'])).rowcount:return
            for item in task['items']:
                if item['id'] not in approved:continue
                args=(self.store.agent_id,task['account'],task['room'],item['id'])
                old=db.execute('SELECT * FROM group_knowledge WHERE agent=? AND account=? AND room=? AND id=?',args).fetchone()
                if old and (old['manual'] or old['status']=='disabled'):continue
                inserted=db.execute('INSERT OR IGNORE INTO group_knowledge_sources VALUES(?,?,?,?,?,?,?)',(*args,task['id'],item['source_id'],stable_id(item['quote']))).rowcount
                if not inserted:continue
                hits=(old['hits'] if old and old['epoch']==task['epoch'] else 0)+1
                status='usable' if item['kind']=='expression' or hits>=policy['jargon_batches'] else 'observing'
                db.execute('INSERT OR REPLACE INTO group_knowledge VALUES(?,?,?,?,?,?,?,?,?,?,?)',(*args,item['kind'],item['term'],item['meaning'],status,hits,0,task['epoch']))

    def failed(self,task,now):
        with self.store.connection() as db:
            db.execute("UPDATE group_learning_batches SET attempts=attempts+1,retry_at=?,stage=CASE WHEN attempts>=1 THEN 'failed' ELSE stage END WHERE agent=? AND id=?",(now+60,self.store.agent_id,task['id']))

    def records(self,account,room):
        with self.store.connection() as db:
            return [dict(r) for r in db.execute('SELECT * FROM group_knowledge WHERE agent=? AND account=? AND room=? AND epoch=? ORDER BY hits DESC LIMIT 100',(self.store.agent_id,account,room,self.store.get_setting('memory_epoch',0)))]

    def sources(self,account,room,key,now):
        with self.store.connection() as db:
            rows=db.execute('''SELECT s.*,b.payload,b.created,b.epoch FROM group_knowledge_sources s
                LEFT JOIN group_learning_batches b ON b.agent=s.agent AND b.id=s.batch
                WHERE s.agent=? AND s.account=? AND s.room=? AND s.item=? LIMIT 10''',(self.store.agent_id,account,room,key)).fetchall()
        result=[]
        for r in rows:
            messages=json.loads(r['payload'] or '{}').get('messages',[]) if (r['created'] or 0)>=now-86400 and r['epoch']==self.store.get_setting('memory_epoch',0) else []
            result.append({'source_id':r['source'],'batch':r['batch'],'quote_hash':r['quote_hash'],
                'evidence':next((m for m in messages if m['event_id']==r['source']),None)})
        return result

    def context(self,account,room,text,policy):
        if not policy['enabled']:return []
        rows=[r for r in self.records(account,room) if r['status']=='usable' and (r['kind']=='expression' or r['term'].casefold() in text.casefold())]
        return [{k:r[k] for k in ('id','kind','term','meaning')} for r in rows[:8]]

    def manage(self,account,room,key,operation,meaning=None):
        if operation not in {'correct','disable','forget'}:raise ValueError('未知学习管理操作')
        if operation=='correct' and (not isinstance(meaning,str) or not 1<=len(meaning.strip())<=300):raise ValueError('更正需1至300字释义')
        epoch=self.store.get_setting('memory_epoch',0)
        with self.store.transaction() as db:
            if not db.execute('SELECT 1 FROM group_knowledge WHERE agent=? AND account=? AND room=? AND id=? AND epoch=?',(self.store.agent_id,account,room,key,epoch)).fetchone():raise ValueError('知识不在当前有效群范围')
            db.execute('UPDATE group_knowledge SET manual=1,status=?,meaning=CASE WHEN ? IS NULL THEN meaning ELSE ? END WHERE agent=? AND account=? AND room=? AND id=?',('usable' if operation=='correct' else 'disabled',meaning,meaning,self.store.agent_id,account,room,key))
        # Cancels output generated with a now-revised knowledge reference.
        self.store.set_setting('memory_epoch',epoch+1)
        with self.store.connection() as db:
            db.execute('UPDATE group_knowledge SET epoch=? WHERE agent=? AND epoch=?',(epoch+1,self.store.agent_id,epoch))
