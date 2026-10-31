"""Bounded, agent-scoped projections for platform management pages."""
import json


class SQLiteConsole:
    def platform_console(self, platform, *, limit=50):
        limit = min(100, max(1, limit))
        with self.connection() as db:
            tasks = db.execute("""SELECT t.*,r.payload AS route_payload,
                COALESCE(json_extract(r.payload,'$.source_platform'),json_extract(e.payload,'$.platform'),t.target_platform) AS source_platform
                FROM tasks t LEFT JOIN task_routes r ON r.agent_id=t.agent_id AND r.task_id=t.task_id
                LEFT JOIN events e ON e.agent_id=t.agent_id AND e.event_id=t.event_id
                WHERE t.agent_id=? AND (source_platform=? OR (r.payload IS NULL AND t.target_platform=?))
                ORDER BY t.created_at DESC,t.task_id LIMIT ?""",
                (self.agent_id,platform,platform,limit)).fetchall()
            task_rows = []
            for row in tasks:
                item = dict(row)
                item['route'] = json.loads(item.pop('route_payload') or '{}')
                task_rows.append(item)
            action_from = """FROM actions a
                LEFT JOIN events e ON e.agent_id=a.agent_id AND e.event_id=json_extract(a.payload,'$.event_id')
                LEFT JOIN task_routes r ON r.agent_id=a.agent_id AND r.task_id=json_extract(a.payload,'$.task_id')
                WHERE a.agent_id=? AND (json_extract(a.payload,'$.platform')=? OR
                    COALESCE(json_extract(r.payload,'$.source_platform'),json_extract(e.payload,'$.platform'))=?)"""
            args = (self.agent_id,platform,platform)
            actions = [dict(r) for r in db.execute("SELECT a.* " + action_from + " ORDER BY a.created_at DESC,a.action_id LIMIT ?",(*args,limit))]
            action_counts = dict(db.execute("SELECT a.state,COUNT(*) " + action_from + " GROUP BY a.state",args))
            records = [dict(r) for r in db.execute("""SELECT * FROM conversation_records
                WHERE agent_id=? AND platform=? AND active=1 AND visibility!='group' ORDER BY occurred_at DESC,record_id DESC LIMIT ?""",
                (self.agent_id,platform,limit))]
            pending = [dict(r) for r in db.execute("""SELECT event_id,payload,state,attempts,next_at,reason FROM events
                WHERE agent_id=? AND json_extract(payload,'$.platform')=? AND state IN ('pending','processing','failed')
                ORDER BY rowid DESC LIMIT ?""",(self.agent_id,platform,limit))]
            counts = dict(db.execute("""SELECT state,COUNT(*) FROM events WHERE agent_id=?
                AND json_extract(payload,'$.platform')=? GROUP BY state""",(self.agent_id,platform)))
            buffered = {'messages':0,'first_at':None,'last_at':None}
            if platform=='qq' and db.execute("SELECT 1 FROM sqlite_master WHERE name='chat_inbox'").fetchone():
                row = db.execute("""SELECT COUNT(*),MIN(received),MAX(received) FROM chat_inbox
                    WHERE agent=? AND state='pending'""",(self.agent_id,)).fetchone()
                buffered = dict(zip(('messages','first_at','last_at'),row))
        return {'tasks':task_rows,'actions':actions,'records':records,'pending_events':pending,
                'event_counts':counts,'action_counts':action_counts,'buffered':buffered,'limit':limit}
