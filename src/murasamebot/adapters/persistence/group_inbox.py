"""Bounded, account/group-scoped context in the existing agent database."""
import json
import time
from statistics import median
from dataclasses import replace
from murasamebot.domain.models import Event, stable_id


class GroupInbox:
    def __init__(self, store):
        self.store=store
        with store.connection() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS group_inbox (
                agent TEXT, id TEXT, account TEXT, room TEXT, received REAL, payload TEXT,
                PRIMARY KEY(agent,id))''')
            db.execute('CREATE INDEX IF NOT EXISTS group_room ON group_inbox(agent,account,room,received)')
            db.execute('''CREATE TABLE IF NOT EXISTS group_turns (
                agent TEXT, account TEXT, room TEXT, revision INTEGER, first_at REAL,last_at REAL,next_at REAL,
                pending TEXT, PRIMARY KEY(agent,account,room))''')
            db.execute('''CREATE TABLE IF NOT EXISTS group_windows (
                agent TEXT,id TEXT,account TEXT,room TEXT,revision INTEGER,created REAL,version TEXT,
                PRIMARY KEY(agent,id))''')
            db.execute('CREATE TABLE IF NOT EXISTS group_seen(agent TEXT,id TEXT,PRIMARY KEY(agent,id))')
            db.execute('CREATE TABLE IF NOT EXISTS group_plans(agent TEXT,action TEXT,window TEXT,PRIMARY KEY(agent,action))')
            db.execute('CREATE TABLE IF NOT EXISTS group_waits(agent TEXT,account TEXT,room TEXT,revision INTEGER,hits INTEGER,PRIMARY KEY(agent,account,room,revision))')
        self.sync_revision()

    def sync_revision(self):
        epoch=self.store.get_setting('memory_epoch',0)
        if self.store.get_setting('group_context_epoch')!=epoch:
            self.reset()
            self.store.set_setting('group_context_epoch',epoch)

    def receive(self,event,triggered,settings,now):
        self.sync_revision()
        with self.store.transaction() as db:
            db.execute('DELETE FROM group_inbox WHERE agent=? AND received<?',(self.store.agent_id,now-settings['context_minutes']*60))
            if db.execute('SELECT 1 FROM memory_suppressed WHERE agent_id=? AND event_id=?',(self.store.agent_id,event.event_id)).fetchone():
                return False
            if settings.get('autonomous_enabled',False) and db.execute('SELECT 1 FROM group_seen WHERE agent=? AND id=?',(self.store.agent_id,event.event_id)).fetchone():return False
            inserted=db.execute('INSERT OR IGNORE INTO group_inbox VALUES(?,?,?,?,?,?)',
                (self.store.agent_id,event.event_id,event.account_id,event.thread_root,now,json.dumps(event.to_dict(),ensure_ascii=False))).rowcount
            if inserted and triggered:
                db.execute("INSERT OR IGNORE INTO events(agent_id,event_id,payload,state) VALUES(?,?,?,'pending')",
                    (self.store.agent_id,event.event_id,json.dumps(event.to_dict(),ensure_ascii=False)))
            fresh=db.execute('INSERT OR IGNORE INTO group_seen VALUES(?,?)',(self.store.agent_id,event.event_id)).rowcount
            if inserted and fresh and settings.get('autonomous_enabled',False):
                db.execute('''INSERT INTO group_turns VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(agent,account,room) DO UPDATE SET revision=revision+1,
                    first_at=COALESCE(first_at,excluded.first_at),last_at=excluded.last_at''',
                    (self.store.agent_id,event.account_id,event.thread_root,1,now,now,0,None))
            db.execute('''DELETE FROM group_inbox WHERE agent=? AND account=? AND room=? AND id NOT IN
                (SELECT id FROM group_inbox WHERE agent=? AND account=? AND room=? ORDER BY received DESC,rowid DESC LIMIT ?)''',
                (self.store.agent_id,event.account_id,event.thread_root,self.store.agent_id,event.account_id,event.thread_root,max(40,settings['context_messages'])))
            return bool(inserted)

    def flush(self,settings,now):
        """Create at most one durable decision per dirty group window, never a new AI loop."""
        self.sync_revision()
        if not settings['enabled'] or not settings['autonomous_enabled']:return 0
        count=0
        with self.store.transaction() as db:
            turns=db.execute('SELECT * FROM group_turns WHERE agent=? AND first_at IS NOT NULL',(self.store.agent_id,)).fetchall()
            for t in turns:
                if t['room'] not in settings['group_ids'] or now<t['next_at']:continue
                rhythm=self.rhythm(t['account'],t['room'])
                quiet=min(settings['quiet_seconds'],max(2,rhythm['median_gap_seconds'])) if rhythm['messages']>=6 else settings['quiet_seconds']
                if now<min(t['last_at']+quiet,t['first_at']+settings['max_wait_seconds']):continue
                if t['pending'] and db.execute("SELECT 1 FROM events WHERE agent_id=? AND event_id=? AND state IN ('pending','processing')",(self.store.agent_id,t['pending'])).fetchone():continue
                args=(self.store.agent_id,t['account'],t['room'])
                used=db.execute('SELECT COUNT(*) FROM group_windows WHERE agent=? AND account=? AND room=? AND created>=?',(*args,now-86400)).fetchone()[0]
                if used>=settings['daily_decision_limit']:continue
                row=db.execute('SELECT payload FROM group_inbox WHERE agent=? AND account=? AND room=? AND received>=? ORDER BY received DESC,rowid DESC LIMIT 1',(*args,now-settings['context_minutes']*60)).fetchone()
                if not row:continue
                event=Event.from_dict(json.loads(row[0]))
                # Unknown sends block this group before spending another model call.
                if db.execute("SELECT 1 FROM actions WHERE agent_id=? AND kind='group_reply' AND json_extract(payload,'$.conversation_id')=? AND (state='unknown' OR (state IN ('accepted','sending') AND COALESCE(sent_at,created_at)>?))",(self.store.agent_id,event.conversation_id,now-settings['cooldown_seconds'])).fetchone():continue
                key=stable_id('group-window',self.store.agent_id,*args[1:],t['revision'],event.event_id,t['next_at'],settings,self.store.get_setting('memory_epoch',0))
                window=replace(event,event_id=key,kind='group_window',text='',reply_to='')
                db.execute('INSERT OR IGNORE INTO group_windows VALUES(?,?,?,?,?,?,?)',(*args[:1],key,*args[1:],t['revision'],now,stable_id(settings)))
                db.execute("INSERT OR IGNORE INTO events(agent_id,event_id,payload,state) VALUES(?,?,?,'pending')",(self.store.agent_id,key,json.dumps(window.to_dict(),ensure_ascii=False)))
                db.execute('UPDATE group_turns SET first_at=NULL,next_at=?,pending=? WHERE agent=? AND account=? AND room=?',(now+settings['decision_interval_seconds'],key,*args))
                count+=1
        return count

    def rhythm(self,account,room):
        with self.store.connection() as db:
            rows=db.execute('SELECT payload,received FROM group_inbox WHERE agent=? AND account=? AND room=? ORDER BY received DESC,rowid DESC LIMIT 30',(self.store.agent_id,account,room)).fetchall()
        gaps=[max(0,rows[i]['received']-rows[i+1]['received']) for i in range(len(rows)-1)]
        return {'messages':len(rows),'participants':len({json.loads(r['payload'])['actor_id'] for r in rows}),
                'median_gap_seconds':round(median(gaps),2) if gaps else 0}

    def defer(self,window,settings,now,seconds):
        if not self.valid_window(window,settings):return False
        with self.store.transaction() as db:
            w=db.execute('SELECT * FROM group_windows WHERE agent=? AND id=?',(self.store.agent_id,window)).fetchone()
            args=(self.store.agent_id,w['account'],w['room'],w['revision'])
            row=db.execute('SELECT hits FROM group_waits WHERE agent=? AND account=? AND room=? AND revision=?',args).fetchone()
            if row and row[0]>=2:return False
            db.execute('INSERT INTO group_waits VALUES(?,?,?,?,1) ON CONFLICT(agent,account,room,revision) DO UPDATE SET hits=hits+1',args)
            db.execute('UPDATE group_turns SET first_at=?,next_at=MAX(next_at,?) WHERE agent=? AND account=? AND room=? AND revision=?',(now-settings['max_wait_seconds'],now+seconds,*args))
        return True

    def valid_window(self,window,settings):
        if not settings['enabled'] or not settings['autonomous_enabled']:return False
        with self.store.connection() as db:
            row=db.execute('''SELECT w.version,w.room,w.created FROM group_windows w JOIN group_turns t
                ON w.agent=t.agent AND w.account=t.account AND w.room=t.room AND w.revision=t.revision
                WHERE w.agent=? AND w.id=?''',(self.store.agent_id,window)).fetchone()
        return bool(row and row['version']==stable_id(settings) and row['room'] in settings['group_ids']
                    and row['created']>=self.store.get_setting('group_context_since',0))

    def plan(self,action,window):
        with self.store.connection() as db:
            db.execute('INSERT OR REPLACE INTO group_plans VALUES(?,?,?)',(self.store.agent_id,action.action_id,window))

    def context(self,event,settings,now):
        self.sync_revision()
        with self.store.connection() as db:
            rows=db.execute('''SELECT g.payload FROM group_inbox g WHERE g.agent=? AND g.account=? AND g.room=? AND g.received>=?
                AND NOT EXISTS (SELECT 1 FROM memory_suppressed s WHERE s.agent_id=g.agent AND s.event_id=g.id)
                ORDER BY g.received DESC,g.rowid DESC LIMIT ?''',
                (self.store.agent_id,event.account_id,event.thread_root,now-settings['context_minutes']*60,settings['context_messages'])).fetchall()
            own=db.execute("""SELECT record_id,text,occurred_at FROM conversation_records WHERE agent_id=? AND account_id=?
                AND conversation_id=? AND visibility='group' AND role='agent' AND active=1 AND occurred_at>=?
                ORDER BY occurred_at DESC LIMIT ?""",(self.store.agent_id,event.account_id,event.conversation_id,
                max(now-settings['context_minutes']*60,self.store.get_setting('group_context_since',0)),settings['context_messages'])).fetchall()
        messages=[Event.from_dict(json.loads(r[0])) for r in reversed(rows)]
        messages += [Event(r['record_id'],event.platform,event.account_id,event.account_id,event.conversation_id,
            'group_outgoing',r['text'],r['occurred_at'],'group',thread_root=event.thread_root,actor_name='丛雨') for r in own]
        return sorted(messages,key=lambda m:m.occurred_at)[-settings['context_messages']:]

    def guard(self,action,settings,now):
        event=self.store.event(action.event_id) if action.event_id else None
        if (not event or event.kind!='group_message' or event.visibility!='group' or action.visibility!='group'
                or not settings['enabled'] or event.thread_root not in settings['group_ids']
                or action.account_id!=event.account_id or action.platform!=event.platform
                or action.conversation_id!=event.conversation_id or action.recipient_id!=event.actor_id
                or action.memory_epoch!=self.store.get_setting('memory_epoch',0)
                or action.reply_to!=str(event.sequence) or action.content or action.mentions
                or action.dedup_key!=stable_id(event.event_id,settings)):
            return '群聊范围、来源或配置已变化'
        with self.store.connection() as db:
            plan=db.execute('SELECT window FROM group_plans WHERE agent=? AND action=?',(self.store.agent_id,action.action_id)).fetchone()
            if plan and not self.valid_window(plan[0],settings):return '群聊窗口已有新消息或已失效'
            if not db.execute('SELECT 1 FROM group_inbox WHERE agent=? AND id=? AND received>=?',
                (self.store.agent_id,event.event_id,now-settings['context_minutes']*60)).fetchone():
                return '群聊上下文已过期或清除'
            rows=db.execute("SELECT state,sent_at,created_at FROM actions WHERE agent_id=? AND kind='group_reply' AND action_id!=? AND json_extract(payload,'$.conversation_id')=? AND state IN ('accepted','sending','unknown')",
                (self.store.agent_id,action.action_id,action.conversation_id)).fetchall()
        if any(r['state']=='unknown' for r in rows):return '该群有结果未知的发送，须先核查'
        if any(now-(r['sent_at'] or r['created_at'])<settings['cooldown_seconds'] for r in rows):return '群聊回复间隔尚未到'
        # Rolling 24h is deliberately conservative across timezone changes.
        if sum((r['sent_at'] or r['created_at'])>=now-86400 for r in rows)>=settings['daily_reply_limit']:
            return '该群24小时回复额度已用完'
        return ''

    def reset(self):
        with self.store.transaction() as db:
            db.execute('DELETE FROM group_inbox WHERE agent=?',(self.store.agent_id,))
            db.execute('DELETE FROM group_turns WHERE agent=?',(self.store.agent_id,))
            db.execute("UPDATE actions SET state='cancelled' WHERE agent_id=? AND kind='group_reply' AND state IN ('draft','queued','reviewing')",(self.store.agent_id,))
        self.store.set_setting('group_context_since',time.time())
