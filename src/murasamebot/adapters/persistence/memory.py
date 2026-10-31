"""SQLite memory projection. Raw events/outbox remain the authoritative records."""
from __future__ import annotations

import json
import re
import sqlite3

from murasamebot.domain.models import Event, stable_id


MEMORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation_records (
 agent_id TEXT NOT NULL, record_id TEXT NOT NULL, event_id TEXT NOT NULL,
 person_id TEXT NOT NULL, scope TEXT NOT NULL, purpose TEXT NOT NULL,
 platform TEXT NOT NULL, account_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
 visibility TEXT NOT NULL, role TEXT NOT NULL, text TEXT NOT NULL,
 occurred_at REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1,
 PRIMARY KEY(agent_id,record_id));
CREATE INDEX IF NOT EXISTS records_scope ON conversation_records(agent_id,person_id,scope,purpose,active,occurred_at);
CREATE TABLE IF NOT EXISTS memory_products (
 agent_id TEXT NOT NULL, product_id TEXT NOT NULL, person_id TEXT NOT NULL,
 scope TEXT NOT NULL, purpose TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL,
 created_at REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1,
 pinned INTEGER NOT NULL DEFAULT 0, supersedes TEXT NOT NULL DEFAULT '',
 PRIMARY KEY(agent_id,product_id));
CREATE TABLE IF NOT EXISTS memory_sources (
 agent_id TEXT NOT NULL, product_id TEXT NOT NULL, record_id TEXT NOT NULL,
 PRIMARY KEY(agent_id,product_id,record_id));
CREATE TABLE IF NOT EXISTS memory_coverage (
 agent_id TEXT NOT NULL, kind TEXT NOT NULL, record_id TEXT NOT NULL,
 PRIMARY KEY(agent_id,kind,record_id));
CREATE TABLE IF NOT EXISTS memory_suppressed (
 agent_id TEXT NOT NULL, event_id TEXT NOT NULL, PRIMARY KEY(agent_id,event_id));
CREATE TABLE IF NOT EXISTS memory_jobs (
 agent_id TEXT NOT NULL, job_id TEXT NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL,
 created_at REAL NOT NULL, detail TEXT NOT NULL DEFAULT '', PRIMARY KEY(agent_id,job_id));
CREATE TABLE IF NOT EXISTS memory_daily (
 agent_id TEXT NOT NULL, day TEXT NOT NULL, calls INTEGER NOT NULL,
 PRIMARY KEY(agent_id,day));
"""


def query_terms(query):
    """Chinese bigrams also recall two-character interests such as 摄影."""
    terms = []
    for word in re.findall(r"[\u4e00-\u9fff]+|[A-Za-z0-9_]+", query[:300].lower()):
        if re.fullmatch(r"[\u4e00-\u9fff]+", word) and len(word) > 2:
            terms.extend(word[i:i+2] for i in range(len(word)-1))
        elif len(word) >= 2:
            terms.append(word)
    return list(dict.fromkeys(terms))[:64]


class SQLiteMemory:
    def _memory_index(self, db):
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_fts'").fetchone():
            return
        try:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_fts'").fetchone():
                db.commit()
                return
            db.execute("CREATE VIRTUAL TABLE memory_fts USING fts5(text,content='conversation_records',content_rowid='rowid',tokenize='trigram')")
            db.execute("CREATE TRIGGER memory_fts_insert AFTER INSERT ON conversation_records BEGIN INSERT INTO memory_fts(rowid,text) VALUES(new.rowid,new.text); END")
            db.execute("CREATE TRIGGER memory_fts_delete AFTER DELETE ON conversation_records BEGIN INSERT INTO memory_fts(memory_fts,rowid,text) VALUES('delete',old.rowid,old.text); END")
            db.execute("CREATE TRIGGER memory_fts_update AFTER UPDATE OF text ON conversation_records BEGIN INSERT INTO memory_fts(memory_fts,rowid,text) VALUES('delete',old.rowid,old.text); INSERT INTO memory_fts(rowid,text) VALUES(new.rowid,new.text); END")
            db.execute("INSERT INTO memory_fts(memory_fts) VALUES('rebuild')")
            db.commit()
        except sqlite3.OperationalError as exc:
            db.rollback()
            # Older Docker SQLite builds retain fully scoped substring recall.
            if "no such module" not in str(exc) and "no such tokenizer" not in str(exc):
                raise

    def _memory_epoch(self, db):
        row = db.execute("SELECT value FROM settings WHERE agent_id=? AND key='memory_epoch'", (self.agent_id,)).fetchone()
        return int(json.loads(row[0])) if row else 0

    def _bump_memory_epoch(self, db):
        epoch = self._memory_epoch(db) + 1
        db.execute("INSERT INTO settings VALUES(?,'memory_epoch',?) ON CONFLICT(agent_id,key) DO UPDATE SET value=excluded.value", (self.agent_id, str(epoch)))
        # A reply based on a deleted/corrected source must be regenerated.
        db.execute("UPDATE actions SET state='cancelled' WHERE agent_id=? AND state IN ('draft','queued','reviewing') AND COALESCE(json_extract(payload,'$.memory_epoch'),-1)>=0", (self.agent_id,))

    def _archive_event(self, db, event, *, role="user", text=None, record_id=None, occurred_at=None):
        if event.kind not in {"chat", "private_message", "mention", "post_mention", "reply", "group_message", "call_message"}:
            return
        if db.execute("SELECT 1 FROM memory_suppressed WHERE agent_id=? AND event_id=?", (self.agent_id,event.event_id)).fetchone():
            return
        source = db.execute("SELECT state FROM events WHERE agent_id=? AND event_id=?", (self.agent_id,event.event_id)).fetchone()
        if not source or source[0] == "deleted":
            return
        if event.kind=="private_message" and db.execute("SELECT 1 FROM audit WHERE agent_id=? AND kind='private_denied' AND instr(detail,?)=1 LIMIT 1", (self.agent_id,event.event_id+":")).fetchone():
            return
        person = db.execute("SELECT person_id FROM bindings WHERE agent_id=? AND platform=? AND external_id=? AND active=1", (self.agent_id,event.platform,event.actor_id)).fetchone()
        person = person[0] if person else f"unlinked:{event.platform}:{event.actor_id}"
        purpose = "private_dialogue" if event.visibility == "private" else "group_dialogue" if event.visibility == "group" else "public_dialogue"
        db.execute("INSERT OR IGNORE INTO conversation_records VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                   (self.agent_id,record_id or stable_id(self.agent_id,"record",event.event_id),event.event_id,person,
                    f"conversation:{event.platform}:{event.conversation_id}",purpose,event.platform,event.account_id,
                    event.conversation_id,event.visibility,role,event.text if text is None else text,
                    event.occurred_at if occurred_at is None else occurred_at))

    def _archive_action(self, db, action, now):
        if action.kind in {"autonomous_post", "proactive_private"} and action.text:
            from dataclasses import asdict
            event_id = stable_id(self.agent_id, "outgoing-source", action.action_id)
            if db.execute("SELECT 1 FROM memory_suppressed WHERE agent_id=? AND event_id=?", (self.agent_id,event_id)).fetchone():
                return
            conversation = action.conversation_id or f"{action.platform}:{action.account_id}:public_posts"
            event = Event(event_id,action.platform,action.account_id,action.account_id,conversation,
                          "outgoing_message",action.text,now,action.visibility)
            db.execute("INSERT OR IGNORE INTO events(agent_id,event_id,payload,state) VALUES(?,?,?,'done')",
                       (self.agent_id,event_id,json.dumps(asdict(event),ensure_ascii=False)))
            row = db.execute("SELECT state FROM events WHERE agent_id=? AND event_id=?", (self.agent_id,event_id)).fetchone()
            if row[0] == "deleted":
                return
            person = action.subject_person_id if action.visibility=="private" else "agent:self"
            db.execute("INSERT OR IGNORE INTO conversation_records VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                (self.agent_id,stable_id(self.agent_id,"outgoing",action.action_id),event_id,person,
                 f"conversation:{action.platform}:{conversation}","private_dialogue" if action.visibility=="private" else "public_dialogue",
                 action.platform,action.account_id,conversation,action.visibility,"agent",action.text,now))
            return
        if not action.event_id or not action.text or action.kind in {"like","coin"}:
            return
        source = db.execute("SELECT payload,state FROM events WHERE agent_id=? AND event_id=?", (self.agent_id,action.event_id)).fetchone()
        if source and source[1] != "deleted":
            event = Event.from_dict(json.loads(source[0]))
            if action.platform != event.platform or action.visibility != event.visibility:
                # A chat-originated public result was spoken on its delivery
                # platform, not as an extra private message on the source chat.
                if db.execute("SELECT 1 FROM memory_suppressed WHERE agent_id=? AND event_id=?",(self.agent_id,event.event_id)).fetchone():
                    return
                person = db.execute("SELECT person_id FROM bindings WHERE agent_id=? AND platform=? AND external_id=? AND active=1",
                    (self.agent_id,event.platform,event.actor_id)).fetchone()
                person = person[0] if person else f"unlinked:{event.platform}:{event.actor_id}"
                conversation = action.conversation_id or (action.content.key if action.content else f"{action.platform}:{action.account_id}:public_posts")
                db.execute("INSERT OR IGNORE INTO conversation_records VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                    (self.agent_id,stable_id(self.agent_id,"outgoing",action.action_id),event.event_id,person,
                     f"conversation:{action.platform}:{conversation}","private_dialogue" if action.visibility=='private' else 'public_dialogue',
                     action.platform,action.account_id,conversation,action.visibility,'agent',action.text,now))
                return
            self._archive_event(db,event,role="agent",text=action.text,
                                record_id=stable_id(self.agent_id,"outgoing",action.action_id),occurred_at=now)

    def memory_backfill(self, limit=200):
        """Checkpoint by rowid; never reprocess events, re-send outbox or revive tombstones."""
        with self.transaction() as db:
            for table, key in (("events","memory_backfill_events"),("actions","memory_backfill_actions")):
                checkpoint = db.execute("SELECT value FROM settings WHERE agent_id=? AND key=?", (self.agent_id,key)).fetchone()
                cursor = int(json.loads(checkpoint[0])) if checkpoint else 0
                rows = db.execute(f"SELECT rowid AS seq,* FROM {table} WHERE agent_id=? AND rowid>? ORDER BY rowid LIMIT ?", (self.agent_id,cursor,limit)).fetchall()
                for row in rows:
                    if table == "events" and row["state"] == "done":
                        self._archive_event(db,Event.from_dict(json.loads(row["payload"])))
                    elif table == "actions" and row["state"] == "accepted":
                        from murasamebot.domain.models import Action
                        self._archive_action(db,Action.from_dict(json.loads(row["payload"])),row["sent_at"] or row["created_at"])
                if rows:
                    db.execute("INSERT INTO settings VALUES(?,?,?) ON CONFLICT(agent_id,key) DO UPDATE SET value=excluded.value", (self.agent_id,key,str(rows[-1]["seq"])))

    def memory_records(self, person_id, scope, purpose, *, query="", limit=20, before=None):
        # Permissions are part of SQL selection, before ranking. No global search.
        where = "agent_id=? AND person_id=? AND scope=? AND purpose=? AND active=1"
        args = [self.agent_id,person_id,scope,purpose]
        if before is not None:
            where += " AND occurred_at<?"
            args.append(before)
        # A literal phrase uses the trigram index when available. Ranking remains
        # local to the permission-filtered result; no global BM25 statistics.
        literal = query.strip().lower()[:300]
        if len(literal)>=3:
            with self.connection() as db:
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_fts'").fetchone():
                    expression = '"'+literal.replace('"','""')+'"'
                    rows = db.execute(f"SELECT * FROM conversation_records WHERE {where} AND rowid IN (SELECT rowid FROM memory_fts WHERE memory_fts MATCH ?) ORDER BY occurred_at DESC LIMIT ?", (*args,expression,min(500,max(1,limit)))).fetchall()
                    if rows:
                        return [dict(r) for r in rows]
        terms = query_terms(query)
        if query and not terms:
            return []
        if terms:
            where += " AND (" + " OR ".join("instr(lower(text),?)>0" for _ in terms) + ")"
            args.extend(terms)
            order = "("+"+".join("(instr(lower(text),?)>0)" for _ in terms)+") DESC,occurred_at DESC,record_id"
            args.extend(terms)
        else:
            order = "occurred_at DESC,record_id"
        with self.connection() as db:
            rows = db.execute(f"SELECT * FROM conversation_records WHERE {where} ORDER BY {order} LIMIT ?", (*args,min(500,max(1,limit)))).fetchall()
        return [dict(row) for row in rows]

    def memory_products(self, person_id, scope, purpose, *, limit=20, query=""):
        terms = query_terms(query)
        where = "agent_id=? AND person_id=? AND scope=? AND purpose=? AND active=1"
        args = [self.agent_id,person_id,scope,purpose]
        if terms:
            where += " AND ("+" OR ".join("instr(lower(text),?)>0" for _ in terms)+")"
            args.extend(terms)
        with self.connection() as db:
            rows = db.execute(f"SELECT * FROM memory_products WHERE {where} ORDER BY pinned DESC,created_at DESC LIMIT ?", (*args,min(100,limit))).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["sources"] = [r[0] for r in db.execute("SELECT record_id FROM memory_sources WHERE agent_id=? AND product_id=?", (self.agent_id,row["product_id"]))]
                result.append(item)
        return result

    def memory_corrections(self, source_ids):
        if not source_ids:
            return []
        with self.connection() as db:
            rows = db.execute("SELECT DISTINCT p.* FROM memory_products p JOIN memory_sources s ON s.agent_id=p.agent_id AND s.product_id=p.product_id WHERE p.agent_id=? AND p.active=1 AND p.kind='correction' AND s.record_id IN ("+",".join("?" for _ in source_ids)+")", (self.agent_id,*source_ids)).fetchall()
            result=[]
            for row in rows:
                item=dict(row)
                item["sources"]=[r[0] for r in db.execute("SELECT record_id FROM memory_sources WHERE agent_id=? AND product_id=?", (self.agent_id,row["product_id"]))]
                result.append(item)
        return result

    def memory_batch(self, kind, *, cutoff, batch_size, min_messages, recent=0, min_characters=0):
        with self.connection() as db:
            cte = "WITH ranked AS (SELECT *,ROW_NUMBER() OVER (PARTITION BY person_id,scope,purpose ORDER BY occurred_at DESC,record_id DESC) AS pos FROM conversation_records WHERE agent_id=? AND active=1), eligible AS (SELECT * FROM ranked r WHERE pos>? AND occurred_at<=? AND NOT EXISTS (SELECT 1 FROM memory_coverage c WHERE c.agent_id=r.agent_id AND c.record_id=r.record_id AND c.kind=?)) "
            params = (self.agent_id,recent,cutoff,kind)
            group = db.execute(cte+"SELECT person_id,scope,purpose FROM eligible GROUP BY person_id,scope,purpose HAVING COUNT(*)>=? OR (? > 0 AND SUM(length(text))>=?) ORDER BY MIN(occurred_at) LIMIT 1", (*params,min_messages,min_characters,min_characters)).fetchone()
            if not group:
                return []
            rows = db.execute(cte+"SELECT * FROM eligible WHERE person_id=? AND scope=? AND purpose=? ORDER BY occurred_at,record_id LIMIT ?", (*params,group["person_id"],group["scope"],group["purpose"],batch_size)).fetchall()
        return [dict(row) for row in rows]

    def memory_reserve(self, day, limit):
        with self.transaction() as db:
            row = db.execute("SELECT calls FROM memory_daily WHERE agent_id=? AND day=?", (self.agent_id,day)).fetchone()
            if (row[0] if row else 0)>=limit:
                return False
            db.execute("INSERT INTO memory_daily VALUES(?,?,1) ON CONFLICT(agent_id,day) DO UPDATE SET calls=calls+1", (self.agent_id,day))
        return True

    def memory_job(self, job_id, kind, state, now, detail=""):
        with self.connection() as db:
            db.execute("INSERT INTO memory_jobs VALUES(?,?,?,?,?,?) ON CONFLICT(agent_id,job_id) DO UPDATE SET state=excluded.state,detail=excluded.detail,created_at=excluded.created_at", (self.agent_id,job_id,kind,state,now,detail[:300]))

    def memory_commit(self, records, products, kind, epoch, now):
        """Atomic source recheck, coverage and products; stale jobs cannot resurrect data."""
        first = records[0]
        with self.transaction() as db:
            if self._memory_epoch(db) != epoch:
                return False
            for source in records:
                row = db.execute("SELECT * FROM conversation_records WHERE agent_id=? AND record_id=? AND active=1", (self.agent_id,source["record_id"])).fetchone()
                if not row or row["text"] != source["text"] or any(row[k]!=first[k] for k in ("person_id","scope","purpose")):
                    return False
                if db.execute("SELECT 1 FROM memory_coverage WHERE agent_id=? AND kind=? AND record_id=?", (self.agent_id,kind,source["record_id"])).fetchone():
                    return False
            allowed = {r["record_id"] for r in records}
            for product in products:
                sources = sorted(set(product["sources"]))
                if not sources or not set(sources)<=allowed:
                    raise ValueError("记忆来源无效")
                # A rebuild is a new generation even if the model produces the
                # same text. Never reactivate a deleted/superseded historical row.
                pid = stable_id(self.agent_id,kind,product["kind"],sources,product["text"],epoch)
                db.execute("INSERT OR IGNORE INTO memory_products VALUES(?,?,?,?,?,?,?,?,1,0,'')", (self.agent_id,pid,first["person_id"],first["scope"],first["purpose"],product["kind"],product["text"],now))
                for rid in sources:
                    db.execute("INSERT OR IGNORE INTO memory_sources VALUES(?,?,?)", (self.agent_id,pid,rid))
                for previous in product.get("supersedes",[]):
                    old = db.execute("SELECT person_id,scope,purpose,kind FROM memory_products WHERE agent_id=? AND product_id=? AND active=1", (self.agent_id,previous)).fetchone()
                    if not old or old["kind"] not in {"fact"} or any(old[k]!=first[k] for k in ("person_id","scope","purpose")):
                        raise ValueError("不能修订其他范围的事实")
                    # Preserve provenance transitively, so forgetting the old
                    # source also invalidates the fact derived from its revision.
                    db.execute("INSERT OR IGNORE INTO memory_sources SELECT agent_id,?,record_id FROM memory_sources WHERE agent_id=? AND product_id=?", (pid,self.agent_id,previous))
                    db.execute("UPDATE memory_products SET active=0 WHERE agent_id=? AND product_id=?", (self.agent_id,previous))
                    db.execute("UPDATE memory_products SET supersedes=? WHERE agent_id=? AND product_id=?", (previous,self.agent_id,pid))
            if any(p.get("supersedes") for p in products):
                self._bump_memory_epoch(db)
            for rid in allowed:
                db.execute("INSERT OR IGNORE INTO memory_coverage VALUES(?,?,?)", (self.agent_id,kind,rid))
        return True

    def _invalidate_memory_sources(self, db, event_ids):
        self.social_invalidate_events(db,event_ids)
        for eid in set(event_ids):
            db.execute("INSERT OR IGNORE INTO memory_suppressed VALUES(?,?)", (self.agent_id,eid))
            ids = [r[0] for r in db.execute("SELECT record_id FROM conversation_records WHERE agent_id=? AND event_id=?", (self.agent_id,eid))]
            for rid in ids:
                # Invalidate whole summaries even if only one of their sources changed.
                products = [r[0] for r in db.execute("SELECT product_id FROM memory_sources WHERE agent_id=? AND record_id=?", (self.agent_id,rid))]
                for pid in products:
                    db.execute("UPDATE memory_products SET active=0,text='[已删除]' WHERE agent_id=? AND product_id=?", (self.agent_id,pid))
                    db.execute("DELETE FROM memory_coverage WHERE agent_id=? AND record_id IN (SELECT record_id FROM memory_sources WHERE agent_id=? AND product_id=?)", (self.agent_id,self.agent_id,pid))
                db.execute("UPDATE conversation_records SET active=0,text='[已删除]' WHERE agent_id=? AND record_id=?", (self.agent_id,rid))
        self._bump_memory_epoch(db)

    def memory_edit(self, product_id, *, text=None, pinned=None, forget=False, now=0):
        with self.transaction() as db:
            row = db.execute("SELECT * FROM memory_products WHERE agent_id=? AND product_id=? AND active=1", (self.agent_id,product_id)).fetchone()
            if not row:
                raise ValueError("记忆条目不存在或已失效")
            if row["purpose"] == "owner_observation_diary":
                if not forget:
                    raise ValueError("观察日记仅支持查看来源和遗忘，不改写观看记录")
                db.execute("UPDATE memory_products SET active=0,text='[已删除]' WHERE agent_id=? AND product_id=?",(self.agent_id,product_id))
                self._bump_memory_epoch(db)
                return product_id
            if forget:
                eids = [r[0] for r in db.execute("SELECT DISTINCT r.event_id FROM conversation_records r JOIN memory_sources s ON s.agent_id=r.agent_id AND s.record_id=r.record_id WHERE s.agent_id=? AND s.product_id=?", (self.agent_id,product_id))]
            elif text is not None:
                if not isinstance(text,str) or not text.strip() or len(text)>4000:
                    raise ValueError("更正文字需为 1 至 4000 字")
                pid = stable_id(self.agent_id,"correction",product_id,text,now)
                db.execute("UPDATE memory_products SET active=0 WHERE agent_id=? AND product_id=?", (self.agent_id,product_id))
                db.execute("INSERT INTO memory_products VALUES(?,?,?,?,?,?,?,?,1,?,?)", (self.agent_id,pid,row["person_id"],row["scope"],row["purpose"],"correction",text.strip(),now,row["pinned"],product_id))
                db.execute("INSERT INTO memory_sources SELECT agent_id,?,record_id FROM memory_sources WHERE agent_id=? AND product_id=?", (pid,self.agent_id,product_id))
                db.execute("UPDATE memory_products SET active=0 WHERE agent_id=? AND product_id!=? AND product_id IN (SELECT s.product_id FROM memory_sources s WHERE s.agent_id=? AND s.record_id IN (SELECT record_id FROM memory_sources WHERE agent_id=? AND product_id=?))", (self.agent_id,pid,self.agent_id,self.agent_id,pid))
                related = [r[0] for r in db.execute("SELECT DISTINCT r.event_id FROM conversation_records r JOIN memory_sources s ON s.agent_id=r.agent_id AND s.record_id=r.record_id WHERE s.agent_id=? AND s.product_id=?", (self.agent_id,product_id))]
                self.social_invalidate_events(db,related)
                self._bump_memory_epoch(db)
                return pid
            elif pinned is not None:
                if type(pinned) is not bool:
                    raise ValueError("置顶开关应为布尔值")
                db.execute("UPDATE memory_products SET pinned=? WHERE agent_id=? AND product_id=?", (int(pinned),self.agent_id,product_id))
                return product_id
            else:
                raise ValueError("未指定记忆操作")
            # Forget all sources and their tasks in the same transaction.
            for eid in eids:
                self._forget_source(db,eid)
        return product_id

    def memory_status(self):
        with self.connection() as db:
            counts = {table:db.execute(f"SELECT COUNT(*) FROM {table} WHERE agent_id=? AND active=1", (self.agent_id,)).fetchone()[0] for table in ("conversation_records","memory_products")}
            counts["pending_summary"] = db.execute("SELECT COUNT(*) FROM conversation_records r WHERE agent_id=? AND active=1 AND NOT EXISTS (SELECT 1 FROM memory_coverage c WHERE c.agent_id=r.agent_id AND c.record_id=r.record_id AND c.kind='summary')", (self.agent_id,)).fetchone()[0]
            counts["epoch"] = self._memory_epoch(db)
            counts["fts5_trigram"] = bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_fts'").fetchone())
        counts["database_bytes"] = 0
        for path in (self.path,self.path.with_name(self.path.name+"-wal")):
            try:
                counts["database_bytes"] += path.stat().st_size
            except FileNotFoundError:
                # SQLite can remove WAL after exists() during a concurrent close.
                pass
        return counts

    def memory_contexts(self):
        with self.connection() as db:
            rows = db.execute("SELECT key,value FROM settings WHERE agent_id=? AND key LIKE 'memory_context:%' ORDER BY rowid DESC LIMIT 10", (self.agent_id,)).fetchall()
        return [{"scope_key":r[0],**json.loads(r[1])} for r in rows]

    def memory_source_records(self, source_ids):
        if not isinstance(source_ids,list) or len(source_ids)>500 or any(not isinstance(s,str) for s in source_ids):
            raise ValueError("来源ID列表无效")
        with self.connection() as db:
            rows = db.execute("SELECT * FROM conversation_records WHERE agent_id=? AND record_id IN ("+",".join("?" for _ in source_ids)+")", (self.agent_id,*source_ids)).fetchall()
        return [dict(r) for r in rows]

    def memory_product_source_records(self, product_id):
        with self.connection() as db:
            observations = db.execute("SELECT h.record_id,h.payload FROM observation_diary_sources s JOIN owner_history h ON h.agent_id=s.agent_id AND h.person_id=s.person_id AND h.record_id=s.record_id JOIN memory_products p ON p.agent_id=s.agent_id AND p.product_id=s.product_id AND p.active=1 WHERE s.agent_id=? AND s.product_id=? ORDER BY h.viewed_at",(self.agent_id,product_id)).fetchall()
            if observations:
                result=[]
                for row in observations:
                    item=json.loads(row["payload"])
                    result.append({"record_id":row["record_id"],"role":"observation","active":1,"source_type":"owner_history",
                                   "text":item["title"]+" · "+item.get("category","")+"（观看线索，不代表看完或正在观看）",
                                   "occurred_at":item["viewed_at"]})
                return result
            rows = db.execute("SELECT r.* FROM conversation_records r JOIN memory_sources s ON s.agent_id=r.agent_id AND s.record_id=r.record_id WHERE s.agent_id=? AND s.product_id=? ORDER BY r.occurred_at,r.record_id", (self.agent_id,product_id)).fetchall()
        return [dict(r) for r in rows]

    def memory_recover_jobs(self, now):
        with self.connection() as db:
            db.execute("UPDATE memory_jobs SET state='interrupted',detail='上次维护中断；原文与检查点保留，等待重试' WHERE agent_id=? AND state='running' AND created_at<?", (self.agent_id,now-900))

    def memory_rebuild(self, person_id, scope, purpose):
        with self.transaction() as db:
            db.execute("UPDATE memory_products SET active=0 WHERE agent_id=? AND person_id=? AND scope=? AND purpose=? AND kind!='correction'", (self.agent_id,person_id,scope,purpose))
            db.execute("DELETE FROM memory_coverage WHERE agent_id=? AND record_id IN (SELECT record_id FROM conversation_records WHERE agent_id=? AND person_id=? AND scope=? AND purpose=? AND active=1)", (self.agent_id,self.agent_id,person_id,scope,purpose))
            self._bump_memory_epoch(db)
        self.set_setting("memory_retry_at",0)

    def memory_scopes(self):
        with self.connection() as db:
            rows = db.execute("SELECT person_id,scope,purpose,platform,conversation_id,visibility,COUNT(*) AS messages,MIN(occurred_at) AS first_at,MAX(occurred_at) AS last_at FROM conversation_records WHERE agent_id=? AND active=1 GROUP BY person_id,scope,purpose,platform,conversation_id,visibility ORDER BY last_at DESC", (self.agent_id,)).fetchall()
        return [dict(r) for r in rows]
