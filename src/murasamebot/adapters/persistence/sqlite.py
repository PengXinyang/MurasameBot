from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import time
from threading import Lock
from typing import Any
from uuid import uuid4

from murasamebot.domain.models import Action, ContentRef, Document, Event, ModelError, Receipt, stable_id
from murasamebot.adapters.persistence.memory import MEMORY_SCHEMA, SQLiteMemory, query_terms


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
INSERT INTO schema_version SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM schema_version);
CREATE TABLE IF NOT EXISTS settings (
 agent_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY(agent_id,key));
CREATE TABLE IF NOT EXISTS bindings (
 agent_id TEXT NOT NULL, person_id TEXT NOT NULL, platform TEXT NOT NULL,
 external_id TEXT NOT NULL, display_name TEXT NOT NULL, purpose TEXT NOT NULL,
 version INTEGER NOT NULL DEFAULT 1, active INTEGER NOT NULL DEFAULT 1,
 PRIMARY KEY(agent_id,person_id,platform), UNIQUE(agent_id,platform,external_id));
CREATE TABLE IF NOT EXISTS events (
 agent_id TEXT NOT NULL, event_id TEXT NOT NULL, payload TEXT NOT NULL,
 state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,
 next_at REAL NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '', PRIMARY KEY(agent_id,event_id));
CREATE TABLE IF NOT EXISTS memories (
 agent_id TEXT NOT NULL, memory_id TEXT NOT NULL, person_id TEXT NOT NULL,
 scope TEXT NOT NULL, purpose TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL,
 event_id TEXT NOT NULL, created_at REAL NOT NULL, valid_until REAL, active INTEGER NOT NULL DEFAULT 1,
 PRIMARY KEY(agent_id,memory_id));
CREATE INDEX IF NOT EXISTS memories_scope ON memories(agent_id,person_id,scope,purpose,active);
CREATE TABLE IF NOT EXISTS tasks (
 agent_id TEXT NOT NULL, task_id TEXT NOT NULL, person_id TEXT NOT NULL,
 topic TEXT NOT NULL, event_id TEXT NOT NULL, memory_id TEXT NOT NULL, created_at REAL NOT NULL,
 due_at REAL NOT NULL, expires_at REAL NOT NULL, state TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
 attempts INTEGER NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,
 binding_version INTEGER NOT NULL, target_platform TEXT NOT NULL, interval_days INTEGER NOT NULL,
 sensitive INTEGER NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '', PRIMARY KEY(agent_id,task_id));
CREATE TABLE IF NOT EXISTS actions (
 agent_id TEXT NOT NULL, action_id TEXT NOT NULL, payload TEXT NOT NULL,
 content_key TEXT NOT NULL, dedup_key TEXT NOT NULL, kind TEXT NOT NULL,
 state TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL,
 sent_at REAL, lease_until REAL NOT NULL DEFAULT 0, receipt TEXT NOT NULL DEFAULT '{}',
 PRIMARY KEY(agent_id,action_id));
CREATE INDEX IF NOT EXISTS actions_content ON actions(agent_id,content_key,state);
CREATE TABLE IF NOT EXISTS documents (
 agent_id TEXT NOT NULL, content_key TEXT NOT NULL, payload TEXT NOT NULL,
 created_at REAL NOT NULL, PRIMARY KEY(agent_id,content_key));
CREATE TABLE IF NOT EXISTS views (
 agent_id TEXT NOT NULL, content_key TEXT NOT NULL, created_at REAL NOT NULL,
 reason TEXT NOT NULL, PRIMARY KEY(agent_id,content_key));
CREATE TABLE IF NOT EXISTS leases (
 agent_id TEXT NOT NULL, name TEXT NOT NULL, holder TEXT NOT NULL, until_at REAL NOT NULL,
 PRIMARY KEY(agent_id,name));
CREATE TABLE IF NOT EXISTS model_daily (
 agent_id TEXT NOT NULL, day TEXT NOT NULL, calls INTEGER NOT NULL,
 PRIMARY KEY(agent_id,day));
CREATE TABLE IF NOT EXISTS model_usage (
 id INTEGER PRIMARY KEY, agent_id TEXT NOT NULL, operation TEXT NOT NULL, model TEXT NOT NULL,
 tokens_in INTEGER NOT NULL, tokens_out INTEGER NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS audit (
 id INTEGER PRIMARY KEY, agent_id TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL, created_at REAL NOT NULL);
"""

SCHEMA_LOCK = Lock()


class SQLiteStore(SQLiteMemory):
    def __init__(self, path: str | Path, agent_id: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.agent_id = agent_id
        self.holder = uuid4().hex
        # Multiple dashboard requests can open repositories concurrently. DDL and a
        # journal-mode switch must never be repeated on every request.
        with SCHEMA_LOCK, self.connection() as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'").fetchone()
            if not exists:
                if db.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                    db.execute("PRAGMA journal_mode=WAL")
                db.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + "\nCOMMIT;")
            version = db.execute("SELECT version FROM schema_version").fetchone()[0]
            if version == 1:
                # SQLite backup includes committed WAL pages. Keep a recoverable
                # v1 copy before the transactional migration; never overwrite it.
                backup = self.path.with_name(self.path.name + ".v1-backup")
                if not backup.exists():
                    with sqlite3.connect(backup) as target:
                        db.backup(target)
                db.executescript("BEGIN IMMEDIATE;\n" + MEMORY_SCHEMA + "\nUPDATE schema_version SET version=2;\nCOMMIT;")
                version = 2
            if version != 2:
                raise RuntimeError(f"不兼容的数据库版本：{version}")
            self._memory_index(db)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    def get_setting(self, key, default=None):
        with self.connection() as db:
            row = db.execute("SELECT value FROM settings WHERE agent_id=? AND key=?", (self.agent_id, key)).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self, key, value):
        with self.connection() as db:
            db.execute("INSERT INTO settings VALUES(?,?,?) ON CONFLICT(agent_id,key) DO UPDATE SET value=excluded.value", (self.agent_id, key, dumps(value)))

    def bind(self, person_id, platform, external_id, display_name="", purpose="owner_video_recommendation"):
        if not all((person_id, platform, external_id)):
            raise ValueError("人物、平台和稳定账号标识不能为空")
        with self.transaction() as db:
            old = db.execute("SELECT * FROM bindings WHERE agent_id=? AND person_id=? AND platform=?", (self.agent_id, person_id, platform)).fetchone()
            if old and old["active"] and old["external_id"] == external_id and old["purpose"] == purpose:
                if display_name:
                    db.execute("UPDATE bindings SET display_name=? WHERE agent_id=? AND person_id=? AND platform=?", (display_name, self.agent_id, person_id, platform))
                return
            version = old["version"] + 1 if old else 1
            db.execute("INSERT INTO bindings VALUES(?,?,?,?,?,?,?,1) ON CONFLICT(agent_id,person_id,platform) DO UPDATE SET external_id=excluded.external_id,display_name=excluded.display_name,purpose=excluded.purpose,version=excluded.version,active=1", (self.agent_id, person_id, platform, external_id, display_name, purpose, version))
            if old:
                self._invalidate_person(db, person_id, platform)

    def resolve_person(self, platform, external_id):
        with self.connection() as db:
            row = db.execute("SELECT person_id FROM bindings WHERE agent_id=? AND platform=? AND external_id=? AND active=1", (self.agent_id, platform, external_id)).fetchone()
        return row[0] if row else None

    def binding(self, person_id, platform):
        with self.connection() as db:
            row = db.execute("SELECT * FROM bindings WHERE agent_id=? AND person_id=? AND platform=? AND active=1", (self.agent_id, person_id, platform)).fetchone()
        return dict(row) if row else None

    def _invalidate_person(self, db, person_id, platform):
        events = [r[0] for r in db.execute("SELECT event_id FROM conversation_records WHERE agent_id=? AND person_id=? AND platform=? AND active=1 UNION SELECT m.event_id FROM memories m JOIN events e ON e.agent_id=m.agent_id AND e.event_id=m.event_id WHERE m.agent_id=? AND m.person_id=? AND json_extract(e.payload,'$.platform')=?", (self.agent_id,person_id,platform,self.agent_id,person_id,platform))]
        self._invalidate_memory_sources(db,events)
        db.execute("UPDATE memories SET active=0 WHERE agent_id=? AND person_id=? AND event_id IN (SELECT event_id FROM events WHERE agent_id=? AND json_extract(payload,'$.platform')=?)", (self.agent_id,person_id,self.agent_id,platform))
        db.execute("UPDATE tasks SET state='cancelled',version=version+1,reason='binding_changed' WHERE agent_id=? AND person_id=? AND (target_platform=? OR event_id IN (SELECT event_id FROM events WHERE agent_id=? AND json_extract(payload,'$.platform')=?)) AND state NOT IN ('completed','cancelled','expired','failed')", (self.agent_id, person_id, platform, self.agent_id, platform))
        db.execute("UPDATE actions SET state='cancelled' WHERE agent_id=? AND state IN ('draft','queued') AND json_extract(payload,'$.task_id') IN (SELECT task_id FROM tasks WHERE agent_id=? AND person_id=? AND state='cancelled')", (self.agent_id, self.agent_id, person_id))
        for row in db.execute("SELECT action_id,payload FROM actions WHERE agent_id=? AND state IN ('draft','queued')", (self.agent_id,)).fetchall():
            action = Action.from_dict(json.loads(row["payload"]))
            if (person_id in action.mentions or person_id == action.subject_person_id) and action.platform == platform:
                db.execute("UPDATE actions SET state='cancelled' WHERE agent_id=? AND action_id=?", (self.agent_id, row["action_id"]))

    def revoke_binding(self, person_id, platform):
        with self.transaction() as db:
            db.execute("UPDATE bindings SET active=0,version=version+1 WHERE agent_id=? AND person_id=? AND platform=?", (self.agent_id, person_id, platform))
            self._invalidate_person(db, person_id, platform)

    def enqueue_event(self, event, *, baseline=False):
        with self.connection() as db:
            if db.execute("SELECT 1 FROM memory_suppressed WHERE agent_id=? AND event_id=?", (self.agent_id,event.event_id)).fetchone():
                return False
            cur = db.execute("INSERT OR IGNORE INTO events(agent_id,event_id,payload,state) VALUES(?,?,?,?)", (self.agent_id, event.event_id, dumps(event.to_dict()), "baseline" if baseline else "pending"))
            if not cur.rowcount and event.share_content:
                previous = db.execute("SELECT payload,state FROM events WHERE agent_id=? AND event_id=?", (self.agent_id, event.event_id)).fetchone()
                if previous and previous["state"] != "deleted":
                    old = Event.from_dict(json.loads(previous["payload"]))
                    if (old.share_content is None and old.platform == event.platform and old.account_id == event.account_id and
                            old.actor_id == event.actor_id and old.content == event.content):
                        # Enrich legacy events with a verified parent reference;
                        # never reset processing state, replay text or revive deletes.
                        db.execute("UPDATE events SET payload=json_set(payload,'$.share_content',json(?)) WHERE agent_id=? AND event_id=?",
                                   (dumps(asdict(event.share_content)), self.agent_id, event.event_id))
            return cur.rowcount == 1

    def claim_event(self, now):
        with self.transaction() as db:
            db.execute("UPDATE events SET state='pending' WHERE agent_id=? AND state='processing' AND lease_until<?", (self.agent_id, now))
            def ordering_key(alias):
                return f"COALESCE((SELECT 'person:'||b.person_id FROM bindings b WHERE b.agent_id={alias}.agent_id AND b.platform=json_extract({alias}.payload,'$.platform') AND b.external_id=json_extract({alias}.payload,'$.actor_id') AND b.active=1 AND json_extract({alias}.payload,'$.visibility')='private'),'conversation:'||json_extract({alias}.payload,'$.conversation_id'))"
            row = db.execute(f"SELECT * FROM events WHERE agent_id=? AND state='pending' AND next_at<=? AND NOT EXISTS (SELECT 1 FROM events earlier WHERE earlier.agent_id=events.agent_id AND earlier.event_id!=events.event_id AND {ordering_key('earlier')}={ordering_key('events')} AND (earlier.state='processing' OR (earlier.state='pending' AND earlier.rowid<events.rowid))) AND NOT EXISTS (SELECT 1 FROM settings s WHERE s.agent_id=events.agent_id AND s.key='platform_paused:'||json_extract(events.payload,'$.platform') AND s.value='true') ORDER BY CASE WHEN json_extract(payload,'$.kind') IN ('mention','post_mention','reply','private_message','chat') THEN 0 ELSE 1 END,rowid LIMIT 1", (self.agent_id, now)).fetchone()
            if not row:
                return None
            db.execute("UPDATE events SET state='processing',attempts=attempts+1,lease_until=? WHERE agent_id=? AND event_id=?", (now + 900, self.agent_id, row["event_id"]))
            return Event.from_dict(json.loads(row["payload"]))

    def finish_event(self, event_id, state, reason=""):
        with self.connection() as db:
            db.execute("UPDATE events SET state=?,reason=?,lease_until=0 WHERE agent_id=? AND event_id=? AND state!='deleted'", (state, reason[:500], self.agent_id, event_id))
            if state == "done":
                row = db.execute("SELECT payload FROM events WHERE agent_id=? AND event_id=? AND state='done'", (self.agent_id,event_id)).fetchone()
                if row:
                    self._archive_event(db,Event.from_dict(json.loads(row[0])))

    def event(self, event_id):
        with self.connection() as db:
            row = db.execute("SELECT payload FROM events WHERE agent_id=? AND event_id=? AND state!='deleted'", (self.agent_id, event_id)).fetchone()
        return Event.from_dict(json.loads(row[0])) if row else None

    def retry_event(self, event_id, now, reason, max_attempts):
        with self.transaction() as db:
            row = db.execute("SELECT attempts FROM events WHERE agent_id=? AND event_id=?", (self.agent_id, event_id)).fetchone()
            if row:
                state = "failed" if row[0] >= max_attempts else "pending"
                db.execute("UPDATE events SET state=?,reason=?,next_at=?,lease_until=0 WHERE agent_id=? AND event_id=? AND state!='deleted'", (state, reason[:500], now + min(3600, 120 * 2 ** row[0]), self.agent_id, event_id))

    def memory(self, person_id, scope, purpose, now, *, limit=8, query=""):
        # Scope and purpose filtering happens in SQL, before any relevance ranking.
        if purpose in {"public_dialogue", "private_dialogue"}:
            scopes = [scope]
        elif purpose == "owner_dialogue":
            scopes = [scope, "person"]
        else:
            scopes = ["person"]
        placeholders = ",".join("?" for _ in scopes)
        allowed_purposes = [purpose]
        if purpose == "owner_dialogue":
            allowed_purposes += ["owner_video_recommendation", "owner_private"]
        pp = ",".join("?" for _ in allowed_purposes)
        terms=query_terms(query)
        extra=" AND ("+" OR ".join("instr(lower(text),?)>0" for _ in terms)+")" if terms else ""
        order="("+"+".join("(instr(lower(text),?)>0)" for _ in terms)+") DESC," if terms else ""
        with self.connection() as db:
            rows = db.execute(f"SELECT memory_id,text,kind,created_at FROM memories WHERE agent_id=? AND person_id=? AND scope IN ({placeholders}) AND purpose IN ({pp}) AND active=1 AND (valid_until IS NULL OR valid_until>?){extra} ORDER BY {order}created_at DESC LIMIT ?", (self.agent_id, person_id, *scopes, *allowed_purposes, now,*terms,*terms,limit)).fetchall()
        return [dict(row) for row in rows]

    def add_memory(self, person_id, scope, purpose, text, event_id, now, *, kind="preference", valid_until=None):
        memory_id = stable_id(self.agent_id, event_id, kind, purpose, text)
        with self.transaction() as db:
            source = db.execute("SELECT state FROM events WHERE agent_id=? AND event_id=?", (self.agent_id, event_id)).fetchone()
            if (source and source[0] == "deleted") or db.execute("SELECT 1 FROM memory_suppressed WHERE agent_id=? AND event_id=?", (self.agent_id,event_id)).fetchone():
                raise ValueError("来源已删除")
            db.execute("INSERT OR IGNORE INTO memories VALUES(?,?,?,?,?,?,?,?,?,?,1)", (self.agent_id, memory_id, person_id, scope, purpose, kind, text, event_id, now, valid_until))
        return memory_id

    def forget(self, memory_id):
        with self.connection() as db:
            row = db.execute("SELECT event_id FROM memories WHERE agent_id=? AND memory_id=?", (self.agent_id,memory_id)).fetchone()
        if row:
            self.forget_source(row[0])

    def forget_source(self, event_id):
        with self.transaction() as db:
            self._forget_source(db,event_id)

    def _forget_source(self, db, event_id):
        row = db.execute("SELECT event_id FROM events WHERE agent_id=? AND event_id=? UNION SELECT event_id FROM memories WHERE agent_id=? AND event_id=?", (self.agent_id,event_id,self.agent_id,event_id)).fetchone()
        if not row:
            return
        self._invalidate_memory_sources(db,[event_id])
        # Tombstone the source so replay cannot recreate derived memories or tasks.
        db.execute("UPDATE memories SET active=0,text='[已删除]' WHERE agent_id=? AND event_id=?", (self.agent_id, row[0]))
        db.execute("UPDATE events SET state='deleted',payload=json_set(payload,'$.text','[已删除]') WHERE agent_id=? AND event_id=?", (self.agent_id, row[0]))
        task_ids = db.execute("SELECT task_id FROM tasks WHERE agent_id=? AND event_id=?", (self.agent_id, row[0])).fetchall()
        for task in task_ids:
            self._cancel_task(db, task[0])
        db.execute("UPDATE tasks SET topic='[已删除]' WHERE agent_id=? AND event_id=?", (self.agent_id, row[0]))
        db.execute("UPDATE actions SET state=CASE WHEN state IN ('draft','queued','reviewing') THEN 'cancelled' ELSE state END,payload=json_set(payload,'$.text','[已删除]') WHERE agent_id=? AND json_extract(payload,'$.event_id')=?", (self.agent_id,event_id))
        db.execute("DELETE FROM settings WHERE agent_id=? AND (key LIKE 'memory_context:%' OR key=?)", (self.agent_id,"private_intent:"+event_id))

    def add_task(self, person_id, topic, event_id, memory_id, now, due_at, expires_at, binding_version, *, target_platform, interval_days=0, sensitive=False):
        task_id = stable_id(self.agent_id, "intent", event_id)
        with self.transaction() as db:
            source = db.execute("SELECT state FROM events WHERE agent_id=? AND event_id=?", (self.agent_id, event_id)).fetchone()
            if (source and source[0] == "deleted") or db.execute("SELECT 1 FROM memory_suppressed WHERE agent_id=? AND event_id=?", (self.agent_id,event_id)).fetchone():
                raise ValueError("来源已删除")
            db.execute("INSERT OR IGNORE INTO tasks(agent_id,task_id,person_id,topic,event_id,memory_id,created_at,due_at,expires_at,state,binding_version,target_platform,interval_days,sensitive) VALUES(?,?,?,?,?,?,?,?,?,'scheduled',?,?,?,?)", (self.agent_id, task_id, person_id, topic, event_id, memory_id, now, due_at, expires_at, binding_version, target_platform, interval_days, int(sensitive)))
        return task_id

    def active_tasks(self, person_id):
        with self.connection() as db:
            rows = db.execute("SELECT * FROM tasks WHERE agent_id=? AND person_id=? AND state NOT IN ('completed','cancelled','expired','failed') ORDER BY created_at DESC", (self.agent_id, person_id)).fetchall()
        return [dict(row) for row in rows]

    def _cancel_task(self, db, task_id):
        db.execute("UPDATE tasks SET state='cancelled',version=version+1,reason='cancelled_or_source_deleted' WHERE agent_id=? AND task_id=? AND state!='completed'", (self.agent_id, task_id))
        db.execute("UPDATE actions SET state='cancelled' WHERE agent_id=? AND state IN ('draft','queued') AND json_extract(payload,'$.task_id')=?", (self.agent_id, task_id))

    def cancel_task(self, task_id):
        with self.transaction() as db:
            self._cancel_task(db, task_id)

    def claim_task(self, now):
        with self.transaction() as db:
            db.execute("UPDATE actions SET state='expired' WHERE agent_id=? AND state IN ('draft','queued') AND expires_at<=?", (self.agent_id, now))
            db.execute("UPDATE tasks SET state='waiting',due_at=?,reason='草稿已过期，等待重新筛选' WHERE agent_id=? AND state='awaiting_approval' AND EXISTS (SELECT 1 FROM actions a WHERE a.agent_id=tasks.agent_id AND json_extract(a.payload,'$.task_id')=tasks.task_id AND json_extract(a.payload,'$.task_version')=tasks.version AND a.state='expired') AND NOT EXISTS (SELECT 1 FROM actions a WHERE a.agent_id=tasks.agent_id AND json_extract(a.payload,'$.task_id')=tasks.task_id AND json_extract(a.payload,'$.task_version')=tasks.version AND a.state IN ('draft','queued','sending','accepted','unknown'))", (now, self.agent_id))
            db.execute("UPDATE tasks SET state='expired' WHERE agent_id=? AND expires_at<=? AND state IN ('scheduled','waiting','running','awaiting_approval')", (self.agent_id, now))
            db.execute("UPDATE tasks SET state='waiting' WHERE agent_id=? AND state='running' AND lease_until<?", (self.agent_id, now))
            row = db.execute("SELECT * FROM tasks WHERE agent_id=? AND state IN ('scheduled','waiting') AND due_at<=? AND expires_at>? ORDER BY due_at LIMIT 1", (self.agent_id, now, now)).fetchone()
            if not row:
                return None
            db.execute("UPDATE tasks SET state='running',attempts=attempts+1,lease_until=? WHERE agent_id=? AND task_id=?", (now + 900, self.agent_id, row["task_id"]))
            result = dict(row)
            result["attempts"] += 1
            result["state"] = "running"
            return result

    def task(self, task_id):
        with self.connection() as db:
            row = db.execute("SELECT * FROM tasks WHERE agent_id=? AND task_id=?", (self.agent_id, task_id)).fetchone()
        return dict(row) if row else None

    def defer_task(self, task_id, due_at, reason, *, exhausted=False):
        with self.connection() as db:
            db.execute("UPDATE tasks SET state=?,due_at=?,reason=?,lease_until=0 WHERE agent_id=? AND task_id=? AND state IN ('running','waiting','scheduled')", ("failed" if exhausted else "waiting", due_at, reason[:500], self.agent_id, task_id))

    def has_action_for_content(self, content_key, now):
        with self.connection() as db:
            row = db.execute("SELECT 1 FROM actions WHERE agent_id=? AND content_key=? AND kind NOT IN ('reply','private_reply','owner_repost','like','coin','browse_reply') AND (state IN ('accepted','unknown','sending','reviewing') OR (state IN ('draft','queued') AND expires_at>?)) LIMIT 1", (self.agent_id, content_key, now)).fetchone()
        return bool(row)

    def enqueue_action(self, action, *, draft, now):
        with self.transaction() as db:
            content_key = action.content.key if action.content else f"{action.platform}:conversation:{action.conversation_id}"
            if action.kind not in {"reply", "private_reply"}:
                if action.kind == "browse_reply":
                    exists = db.execute("SELECT 1 FROM actions WHERE agent_id=? AND dedup_key=? AND (state IN ('accepted','unknown','sending','reviewing') OR (state IN ('draft','queued') AND expires_at>?))", (self.agent_id, action.dedup_key, now)).fetchone()
                    if exists:
                        return False
                group = f"kind='{action.kind}'" if action.kind in {"owner_repost", "like", "coin", "browse_reply"} else "kind NOT IN ('reply','private_reply','owner_repost','like','coin','browse_reply')"
                exists = db.execute(f"SELECT 1 FROM actions WHERE agent_id=? AND content_key=? AND {group} AND (state IN ('accepted','unknown','sending','reviewing') OR (state IN ('draft','queued') AND expires_at>?))", (self.agent_id, content_key, now)).fetchone()
                if exists and action.kind != "browse_reply":
                    return False
            if self._validate_action(db, action, now):
                return False
            result = db.execute("INSERT OR IGNORE INTO actions(agent_id,action_id,payload,content_key,dedup_key,kind,state,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?)", (self.agent_id, action.action_id, dumps(action.to_dict()), content_key, action.dedup_key, action.kind, "draft" if draft else "queued", now, action.expires_at))
            if result.rowcount and action.task_id:
                db.execute("UPDATE tasks SET state='awaiting_approval',lease_until=0 WHERE agent_id=? AND task_id=?", (self.agent_id, action.task_id))
            return result.rowcount == 1

    def _validate_action(self, db, action, now):
        if action.memory_epoch >= 0 and action.memory_epoch != self._memory_epoch(db):
            return "memory_sources_invalidated"
        if action.agent_id != self.agent_id:
            return "wrong_agent"
        if action.expires_at <= now:
            return "expired"
        if action.event_id:
            row = db.execute("SELECT state,payload FROM events WHERE agent_id=? AND event_id=?", (self.agent_id, action.event_id)).fetchone()
            if not row or row[0] == "deleted":
                return "source_deleted"
        if action.kind == "private_reply":
            if not action.event_id:
                return "private_source_missing"
            source = Event.from_dict(json.loads(row["payload"]))
            if (source.kind != "private_message" or source.visibility != "private" or action.visibility != "private" or
                    source.platform != action.platform or source.account_id != action.account_id or
                    source.actor_id != action.recipient_id or source.conversation_id != action.conversation_id or
                    source.reply_to != action.reply_to or source.sequence != action.source_sequence or
                    action.content is not None or action.mentions):
                return "private_route_mismatch"
        for person in set(action.mentions) | ({action.subject_person_id} if action.subject_person_id else set()):
            binding = db.execute("SELECT version,purpose FROM bindings WHERE agent_id=? AND person_id=? AND platform=? AND active=1", (self.agent_id, person, action.platform)).fetchone()
            if not binding or binding[0] != action.binding_version or binding[1] != "owner_video_recommendation":
                return "binding_revoked_or_changed"
        if action.task_id:
            task = db.execute("SELECT t.*,m.active AS memory_active FROM tasks t LEFT JOIN memories m ON m.agent_id=t.agent_id AND m.memory_id=t.memory_id WHERE t.agent_id=? AND t.task_id=?", (self.agent_id, action.task_id)).fetchone()
            if not task or task["version"] != action.task_version or task["state"] in ("cancelled", "expired", "completed", "failed") or task["expires_at"] <= now or not task["memory_active"]:
                return "task_invalidated"
        return ""

    def validate_action(self, action, now):
        with self.connection() as db:
            return self._validate_action(db, action, now)

    def claim_action(self, now, daily_limit, daily_recommendations, day_start, min_gap, recommendation_gap, allow_proactive=True, manual_only=False, require_review=False):
        with self.transaction() as db:
            # A crashed write may have reached the platform. Never requeue it.
            stale = db.execute("SELECT action_id,payload FROM actions WHERE agent_id=? AND state='sending' AND lease_until<?", (self.agent_id, now)).fetchall()
            for row in stale:
                db.execute("UPDATE actions SET state='unknown',receipt=? WHERE agent_id=? AND action_id=?", (dumps({"state": "unknown", "detail": "worker_lost_during_send"}), self.agent_id, row["action_id"]))
                task_id = json.loads(row["payload"]).get("task_id")
                if task_id:
                    db.execute("UPDATE tasks SET state='delivery_unknown' WHERE agent_id=? AND task_id=? AND state!='cancelled'", (self.agent_id, task_id))
            db.execute("UPDATE actions SET state='expired' WHERE agent_id=? AND state IN ('draft','queued') AND expires_at<=?", (self.agent_id, now))
            if db.execute("SELECT 1 FROM actions WHERE agent_id=? AND state='sending'", (self.agent_id,)).fetchone():
                return None
            states = "('accepted','unknown','sending')"
            count = db.execute(f"SELECT COUNT(*) FROM actions WHERE agent_id=? AND state IN {states} AND COALESCE(sent_at,created_at)>=?", (self.agent_id, day_start)).fetchone()[0]
            if count >= daily_limit:
                return None
            last = db.execute(f"SELECT MAX(COALESCE(sent_at,created_at)) FROM actions WHERE agent_id=? AND state IN {states}", (self.agent_id,)).fetchone()[0]
            if last is not None and now - last < min_gap:
                return None
            rows = db.execute("SELECT rowid AS queue_order,payload,kind FROM actions WHERE agent_id=? AND state='queued' ORDER BY CASE kind WHEN 'reply' THEN 0 WHEN 'private_reply' THEN 0 WHEN 'recommendation' THEN 1 ELSE 2 END,created_at", (self.agent_id,)).fetchall()
            for row in rows:
                action = Action.from_dict(json.loads(row["payload"]))
                paused = db.execute("SELECT value FROM settings WHERE agent_id=? AND key=?", (self.agent_id, f"platform_paused:{action.platform}")).fetchone()
                if paused and json.loads(paused[0]):
                    continue
                if manual_only and not action.approved_manually:
                    continue
                if require_review and not action.approved_manually and action.kind not in {"like", "coin"} and action.review_state != "passed":
                    continue
                if action.conversation_id and db.execute("SELECT 1 FROM actions WHERE agent_id=? AND rowid<? AND state IN ('queued','reviewing') AND json_extract(payload,'$.conversation_id')=?", (self.agent_id, row["queue_order"], action.conversation_id)).fetchone():
                    continue
                if not allow_proactive and (action.proactive or action.kind in {"recommendation", "browse_comment", "browse_reply"}):
                    continue
                reason = self._validate_action(db, action, now)
                if reason:
                    db.execute("UPDATE actions SET state='cancelled',receipt=? WHERE agent_id=? AND action_id=?", (dumps({"detail": reason}), self.agent_id, action.action_id))
                    continue
                if row["kind"] == "recommendation":
                    rec = db.execute(f"SELECT COUNT(*) FROM actions WHERE agent_id=? AND kind='recommendation' AND state IN {states} AND COALESCE(sent_at,created_at)>=?", (self.agent_id, day_start)).fetchone()
                    last_rec = db.execute(f"SELECT MAX(COALESCE(sent_at,created_at)) FROM actions WHERE agent_id=? AND kind='recommendation' AND state IN {states}", (self.agent_id,)).fetchone()[0]
                    if rec[0] >= daily_recommendations or (last_rec is not None and now - last_rec < recommendation_gap):
                        continue
                db.execute("UPDATE actions SET state='sending',sent_at=?,lease_until=? WHERE agent_id=? AND action_id=?", (now, now + 180, self.agent_id, action.action_id))
                return action
        return None

    def finish_action(self, action_id, receipt, now):
        if receipt.state not in ("accepted", "failed", "unknown"):
            raise ValueError("无效投递结果")
        if receipt.state == "accepted" and not receipt.mention_resolved:
            receipt = Receipt("unknown", receipt.external_id, receipt.detail + "；提及是否生效待核对", False, receipt.pause)
        with self.transaction() as db:
            row = db.execute("SELECT payload FROM actions WHERE agent_id=? AND action_id=? AND state='sending'", (self.agent_id, action_id)).fetchone()
            if not row:
                return
            db.execute("UPDATE actions SET state=?,receipt=?,sent_at=?,lease_until=0 WHERE agent_id=? AND action_id=?", (receipt.state, dumps(asdict(receipt)), now, self.agent_id, action_id))
            action = Action.from_dict(json.loads(row[0]))
            if receipt.state == "accepted":
                self._archive_action(db,action,now)
            if action.task_id:
                task = db.execute("SELECT * FROM tasks WHERE agent_id=? AND task_id=?", (self.agent_id, action.task_id)).fetchone()
                if task and task["state"] != "cancelled":
                    state = {"accepted": "completed", "unknown": "delivery_unknown", "failed": "failed"}[receipt.state]
                    if receipt.state == "accepted" and not receipt.mention_resolved:
                        state = "delivery_unknown"
                    if state == "completed" and task["interval_days"]:
                        db.execute("UPDATE tasks SET state='scheduled',version=version+1,attempts=0,due_at=?,expires_at=?,reason='' WHERE agent_id=? AND task_id=?", (now + task["interval_days"] * 86400, now + (task["interval_days"] + 7) * 86400, self.agent_id, action.task_id))
                    else:
                        db.execute("UPDATE tasks SET state=?,reason=? WHERE agent_id=? AND task_id=?", (state, receipt.detail[:500], self.agent_id, action.task_id))

    def claim_review_action(self, now):
        with self.transaction() as db:
            # These leases cover model-only work; recovery cannot duplicate a social write.
            stale = db.execute("SELECT action_id FROM actions WHERE agent_id=? AND state='reviewing' AND lease_until<?", (self.agent_id, now)).fetchall()
            for row in stale:
                db.execute("UPDATE actions SET state='draft',lease_until=0,payload=json_set(payload,'$.review_state','unavailable','$.review_reason','审核进程中断，等待人工复核') WHERE agent_id=? AND action_id=?", (self.agent_id, row[0]))
            rows = db.execute("SELECT payload FROM actions WHERE agent_id=? AND state='queued' AND kind NOT IN ('like','coin') AND COALESCE(json_extract(payload,'$.approved_manually'),0)=0 AND COALESCE(json_extract(payload,'$.review_state'),'')!='passed' ORDER BY created_at", (self.agent_id,)).fetchall()
            for row in rows:
                action = Action.from_dict(json.loads(row[0]))
                if self._validate_action(db, action, now):
                    continue
                paused = db.execute("SELECT value FROM settings WHERE agent_id=? AND key=?", (self.agent_id, f"platform_paused:{action.platform}")).fetchone()
                if paused and json.loads(paused[0]):
                    continue
                db.execute("UPDATE actions SET state='reviewing',lease_until=? WHERE agent_id=? AND action_id=?", (now+900, self.agent_id, action.action_id))
                return action
        return None

    def record_action_review(self, action_id, state, reason, now):
        if state not in {"passed", "rejected", "unavailable"}:
            raise ValueError("无效模型审核状态")
        with self.transaction() as db:
            row = db.execute("SELECT payload FROM actions WHERE agent_id=? AND action_id=? AND state IN ('sending','reviewing')", (self.agent_id, action_id)).fetchone()
            if not row:
                return
            data = json.loads(row[0])
            data.update(review_state=state, review_reason=reason[:240])
            if state == "passed":
                db.execute("UPDATE actions SET payload=?,state=CASE WHEN state='reviewing' THEN 'queued' ELSE state END,lease_until=CASE WHEN state='reviewing' THEN 0 ELSE lease_until END WHERE agent_id=? AND action_id=?", (dumps(data), self.agent_id, action_id))
            else:
                # Model refusal and model outages both require a deliberate human
                # approval. Never turn them into automatic retries or failed sends.
                db.execute("UPDATE actions SET payload=?,state='draft',sent_at=NULL,lease_until=0,receipt=? WHERE agent_id=? AND action_id=?",
                           (dumps(data), dumps({"detail": "等待人工审核：" + reason[:240]}), self.agent_id, action_id))
            db.execute("INSERT INTO audit(agent_id,kind,detail,created_at) VALUES(?,?,?,?)",
                       (self.agent_id, "model_review", f"{action_id}: {state}；{reason[:240]}", now))

    def approve_action(self, action_id, now):
        with self.transaction() as db:
            row = db.execute("SELECT payload,state FROM actions WHERE agent_id=? AND action_id=?", (self.agent_id, action_id)).fetchone()
            if not row or row["state"] != "draft":
                raise ValueError("仅未过期草稿可批准")
            reason = self._validate_action(db, Action.from_dict(json.loads(row[0])), now)
            if reason:
                raise ValueError(f"草稿不能发送：{reason}")
            db.execute("UPDATE actions SET state='queued',payload=json_set(payload,'$.approved_manually',json('true')) WHERE agent_id=? AND action_id=?", (self.agent_id, action_id))

    def enable_review_mode(self):
        with self.connection() as db:
            db.execute("UPDATE actions SET state='draft' WHERE agent_id=? AND state='queued' AND COALESCE(json_extract(payload,'$.approved_manually'),0)=0", (self.agent_id,))

    def reject_action(self, action_id):
        with self.transaction() as db:
            row = db.execute("SELECT payload FROM actions WHERE agent_id=? AND action_id=? AND state IN ('draft','queued')", (self.agent_id, action_id)).fetchone()
            if not row:
                raise ValueError("该动作已执行、过期或不处于待发送状态")
            db.execute("UPDATE actions SET state='cancelled' WHERE agent_id=? AND action_id=?", (self.agent_id, action_id))
            task_id = json.loads(row[0]).get("task_id")
            if task_id:
                self._cancel_task(db, task_id)

    def resolve_action(self, action_id, state, external_id, now):
        if state not in {"accepted", "failed"} or (state == "accepted" and not external_id):
            raise ValueError("人工确认受理时必须提供实际评论 ID")
        with self.transaction() as db:
            row = db.execute("SELECT state FROM actions WHERE agent_id=? AND action_id=?", (self.agent_id, action_id)).fetchone()
            if not row or row[0] != "unknown":
                raise ValueError("仅能核对结果未知的动作")
            db.execute("UPDATE actions SET state='sending' WHERE agent_id=? AND action_id=?", (self.agent_id, action_id))
        self.finish_action(action_id, Receipt(state, external_id, "操作者已人工核对；公开可见性未由程序证明"), now)

    def context_history(self, conversation_id, *, limit=6):
        with self.connection() as db:
            rows = db.execute("SELECT payload FROM events WHERE agent_id=? AND state='done' AND json_extract(payload,'$.conversation_id')=? ORDER BY rowid DESC LIMIT ?", (self.agent_id, conversation_id, limit)).fetchall()
            actions = db.execute("SELECT payload FROM actions WHERE agent_id=? AND state='accepted' AND json_extract(payload,'$.conversation_id')=? ORDER BY created_at DESC LIMIT ?", (self.agent_id, conversation_id, limit)).fetchall()
        result = [{"speaker": "user", "text": json.loads(r[0])["text"], "time": json.loads(r[0])["occurred_at"]} for r in rows]
        result += [{"speaker": "agent", "text": json.loads(r[0])["text"], "time": json.loads(r[0])["created_at"]} for r in actions]
        return sorted(result, key=lambda row: row["time"])[-limit:]

    def cache_document(self, document, now):
        with self.connection() as db:
            db.execute("INSERT INTO documents VALUES(?,?,?,?) ON CONFLICT(agent_id,content_key) DO UPDATE SET payload=excluded.payload,created_at=excluded.created_at", (self.agent_id, document.ref.key, dumps(asdict(document)), now))

    def cached_document(self, ref, now):
        with self.connection() as db:
            row = db.execute("SELECT payload FROM documents WHERE agent_id=? AND content_key=? AND created_at>?", (self.agent_id, ref.key, now - 86400)).fetchone()
        if row:
            return Document.from_dict(json.loads(row[0]))
        return None

    def has_viewed(self, ref, now):
        with self.connection() as db:
            return bool(db.execute("SELECT 1 FROM views WHERE agent_id=? AND content_key=? AND created_at>?", (self.agent_id, ref.key, now - 7 * 86400)).fetchone())

    def record_view(self, ref, reason, now):
        with self.connection() as db:
            db.execute("INSERT INTO views VALUES(?,?,?,?) ON CONFLICT(agent_id,content_key) DO UPDATE SET created_at=excluded.created_at,reason=excluded.reason", (self.agent_id, ref.key, now, reason[:500]))

    def acquire_lease(self, name, now, duration):
        with self.transaction() as db:
            row = db.execute("SELECT holder,until_at FROM leases WHERE agent_id=? AND name=?", (self.agent_id, name)).fetchone()
            if row and row["until_at"] > now and row["holder"] != self.holder:
                return False
            db.execute("INSERT INTO leases VALUES(?,?,?,?) ON CONFLICT(agent_id,name) DO UPDATE SET holder=excluded.holder,until_at=excluded.until_at", (self.agent_id, name, self.holder, now + duration))
            return True

    def release_lease(self, name):
        with self.connection() as db:
            db.execute("DELETE FROM leases WHERE agent_id=? AND name=? AND holder=?", (self.agent_id, name, self.holder))

    def recover_idle_work(self, now):
        with self.transaction() as db:
            db.execute("UPDATE events SET state='pending',lease_until=0 WHERE agent_id=? AND state='processing' AND lease_until<?", (self.agent_id, now))
            db.execute("UPDATE actions SET state='draft',lease_until=0,payload=json_set(payload,'$.review_state','unavailable','$.review_reason','审核进程中断，等待人工复核') WHERE agent_id=? AND state='reviewing' AND lease_until<?", (self.agent_id, now))
            rows = db.execute("SELECT action_id,payload FROM actions WHERE agent_id=? AND state='sending' AND lease_until<?", (self.agent_id, now)).fetchall()
            for row in rows:
                db.execute("UPDATE actions SET state='unknown',lease_until=0,receipt=? WHERE agent_id=? AND action_id=?", (dumps({"state":"unknown","detail":"worker_lost_during_send"}),self.agent_id,row["action_id"]))
                task_id = json.loads(row["payload"]).get("task_id")
                if task_id:
                    db.execute("UPDATE tasks SET state='delivery_unknown' WHERE agent_id=? AND task_id=? AND state!='cancelled'", (self.agent_id,task_id))

    def queue_status(self):
        with self.connection() as db:
            events = {row[0]: row[1] for row in db.execute("SELECT state,COUNT(*) FROM events WHERE agent_id=? GROUP BY state", (self.agent_id,))}
            actions = {row[0]: row[1] for row in db.execute("SELECT state,COUNT(*) FROM actions WHERE agent_id=? GROUP BY state", (self.agent_id,))}
            reviews = db.execute("SELECT COUNT(*) FROM actions WHERE agent_id=? AND state='queued' AND kind NOT IN ('like','coin') AND COALESCE(json_extract(payload,'$.approved_manually'),0)=0 AND COALESCE(json_extract(payload,'$.review_state'),'')!='passed'", (self.agent_id,)).fetchone()[0]
        return {"pending_events": events.get("pending", 0), "processing_events": events.get("processing", 0),
                "reviewing_actions": actions.get("reviewing", 0), "queued_actions": actions.get("queued", 0), "draft_actions": actions.get("draft", 0), "review_ready_actions": reviews}

    def reserve_model_call(self, day, limit):
        with self.transaction() as db:
            row = db.execute("SELECT calls FROM model_daily WHERE agent_id=? AND day=?", (self.agent_id, day)).fetchone()
            if (row[0] if row else 0) >= limit:
                raise ModelError("已达到每日模型调用上限")
            db.execute("INSERT INTO model_daily VALUES(?,?,1) ON CONFLICT(agent_id,day) DO UPDATE SET calls=calls+1", (self.agent_id, day))

    def model_usage(self, operation, model, tokens_in, tokens_out, now):
        with self.connection() as db:
            db.execute("INSERT INTO model_usage(agent_id,operation,model,tokens_in,tokens_out,created_at) VALUES(?,?,?,?,?,?)", (self.agent_id, operation, model, tokens_in, tokens_out, now))

    def audit(self, kind, detail, now):
        with self.connection() as db:
            db.execute("INSERT INTO audit(agent_id,kind,detail,created_at) VALUES(?,?,?,?)", (self.agent_id, kind, detail[:1000], now))

    def inspect(self, table, limit=100):
        allowed = {"bindings", "events", "memories", "tasks", "actions", "views", "audit", "model_usage", "model_daily", "conversation_records", "memory_products", "memory_jobs", "memory_sources", "memory_daily"}
        if table not in allowed:
            raise ValueError("不支持的数据表")
        with self.connection() as db:
            rows = db.execute(f"SELECT * FROM {table} WHERE agent_id=? ORDER BY rowid DESC LIMIT ?", (self.agent_id, max(1, min(limit, 500)))).fetchall()
        return [dict(row) for row in rows]
