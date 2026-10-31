import json
from uuid import uuid4
from murasamebot.domain.models import Event,stable_id


class CallSessions:
    def __init__(self,store,owner='owner'):
        self.store,self.owner=store,owner
        with store.connection() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS call_sessions(agent TEXT,id TEXT,account TEXT,peer TEXT,binding INTEGER,
                version TEXT,epoch INTEGER,started REAL,expires REAL,revision INTEGER,state TEXT,mode TEXT,window INTEGER,
                PRIMARY KEY(agent,id))''')
            db.execute('CREATE TABLE IF NOT EXISTS call_inputs(agent TEXT,event TEXT,session TEXT,revision INTEGER,PRIMARY KEY(agent,event))')

    def start(self,account,peer,settings,mode,window,now):
        binding=self.store.binding(self.owner,'qq')
        if not binding or binding['external_id']!=peer or not settings['enabled'] or mode not in {'voice','video'} or mode=='video' and not settings['video_enabled']:raise ValueError('通话授权或模式无效')
        self.stop_all()
        key=uuid4().hex
        with self.store.connection() as db:
            db.execute('INSERT INTO call_sessions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(self.store.agent_id,key,account,peer,binding['version'],stable_id(settings),self.store.get_setting('memory_epoch',0),now,now+settings['session_minutes']*60,0,'active',mode,window))
        return self.get(key)

    def get(self,key):
        with self.store.connection() as db:
            row=db.execute('SELECT * FROM call_sessions WHERE agent=? AND id=?',(self.store.agent_id,key)).fetchone()
        return dict(row) if row else None

    def active(self,session,settings,now):
        binding=self.store.binding(self.owner,'qq')
        return bool(session and session['state']=='active' and session['expires']>now and settings['enabled']
            and (session['mode']!='video' or settings['video_enabled']) and session['version']==stable_id(settings)
            and session['epoch']==self.store.get_setting('memory_epoch',0) and binding and binding['external_id']==session['peer']
            and binding['version']==session['binding'] and not self.store.get_setting('paused',False)
            and not self.store.get_setting('platform_paused:qq',False))

    def interrupt(self,key):
        with self.store.transaction() as db:
            rows=db.execute("SELECT payload FROM events WHERE agent_id=? AND state='pending' AND event_id IN (SELECT event FROM call_inputs WHERE agent=? AND session=?)",(self.store.agent_id,self.store.agent_id,key)).fetchall()
            db.execute("UPDATE events SET state='done' WHERE agent_id=? AND state='pending' AND event_id IN (SELECT event FROM call_inputs WHERE agent=? AND session=?)",(self.store.agent_id,self.store.agent_id,key))
            for row in rows:self.store._archive_event(db,Event.from_dict(json.loads(row[0])))
            db.execute("UPDATE call_sessions SET revision=revision+1 WHERE agent=? AND id=? AND state='active'",(self.store.agent_id,key))
            db.execute("UPDATE actions SET state='cancelled' WHERE agent_id=? AND kind='call_reply' AND state IN ('draft','queued','reviewing') AND json_extract(payload,'$.event_id') IN (SELECT event FROM call_inputs WHERE agent=? AND session=?)",(self.store.agent_id,self.store.agent_id,key))
        return self.get(key)['revision']

    def ingest(self,key,revision,text,settings,now):
        session=self.get(key)
        if not self.active(session,settings,now) or revision!=session['revision'] or not isinstance(text,str) or not 1<=len(text.strip())<=2000:return None
        event=Event(stable_id('qq-call',key,revision),'qq',session['account'],session['peer'],
            stable_id('conversation','qq',session['account'],f"qq:{session['account']}:{session['peer']}"),
            'call_message',text.strip(),now,'private',sequence=revision)
        with self.store.transaction() as db:
            db.execute('INSERT OR IGNORE INTO call_inputs VALUES(?,?,?,?)',(self.store.agent_id,event.event_id,key,revision))
            db.execute("INSERT OR IGNORE INTO events(agent_id,event_id,payload,state) VALUES(?,?,?,'pending')",(self.store.agent_id,event.event_id,json.dumps(event.to_dict(),ensure_ascii=False)))
        return event

    def for_event(self,key):
        with self.store.connection() as db:
            r=db.execute('SELECT s.*,i.revision AS input_revision FROM call_inputs i JOIN call_sessions s ON s.agent=i.agent AND s.id=i.session WHERE i.agent=? AND i.event=?',(self.store.agent_id,key)).fetchone()
        return dict(r) if r else None

    def guard(self,action,settings,now):
        session=self.for_event(action.event_id);event=self.store.event(action.event_id)
        if (not self.active(session,settings,now) or not event or event.kind!='call_message' or event.visibility!='private'
            or action.kind!='call_reply' or action.visibility!='private' or action.platform!='qq'
            or action.account_id!=session['account'] or action.recipient_id!=session['peer']
            or action.conversation_id!=event.conversation_id or action.source_sequence!=session['revision']
            or session['input_revision']!=session['revision'] or action.memory_epoch!=session['epoch'] or action.content or action.mentions):return '通话已结束、被打断或授权/来源已变化'
        return ''

    def stop(self,key):
        self.interrupt(key)
        with self.store.connection() as db:db.execute("UPDATE call_sessions SET state='stopped' WHERE agent=? AND id=?",(self.store.agent_id,key))

    def stop_all(self):
        with self.store.connection() as db:ids=[r[0] for r in db.execute("SELECT id FROM call_sessions WHERE agent=? AND state='active'",(self.store.agent_id,))]
        for key in ids:self.stop(key)
