"""Bounded, account/group-scoped context in the existing agent database."""
import json
import time
from murasamebot.domain.models import Event, stable_id


class GroupInbox:
    def __init__(self, store):
        self.store=store
        with store.connection() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS group_inbox (
                agent TEXT, id TEXT, account TEXT, room TEXT, received REAL, payload TEXT,
                PRIMARY KEY(agent,id))''')
            db.execute('CREATE INDEX IF NOT EXISTS group_room ON group_inbox(agent,account,room,received)')
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
            inserted=db.execute('INSERT OR IGNORE INTO group_inbox VALUES(?,?,?,?,?,?)',
                (self.store.agent_id,event.event_id,event.account_id,event.thread_root,now,json.dumps(event.to_dict(),ensure_ascii=False))).rowcount
            if inserted and triggered:
                db.execute("INSERT OR IGNORE INTO events(agent_id,event_id,payload,state) VALUES(?,?,?,'pending')",
                    (self.store.agent_id,event.event_id,json.dumps(event.to_dict(),ensure_ascii=False)))
            db.execute('''DELETE FROM group_inbox WHERE agent=? AND account=? AND room=? AND id NOT IN
                (SELECT id FROM group_inbox WHERE agent=? AND account=? AND room=? ORDER BY received DESC,rowid DESC LIMIT ?)''',
                (self.store.agent_id,event.account_id,event.thread_root,self.store.agent_id,event.account_id,event.thread_root,settings['context_messages']))
            return bool(inserted)

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
            db.execute("UPDATE actions SET state='cancelled' WHERE agent_id=? AND kind='group_reply' AND state IN ('draft','queued','reviewing')",(self.store.agent_id,))
        self.store.set_setting('group_context_since',time.time())
