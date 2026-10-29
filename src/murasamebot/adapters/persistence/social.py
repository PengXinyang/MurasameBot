"""Social provenance and short-lived role states, isolated from factual memory."""
import json

from murasamebot.domain.models import Event, stable_id


SOCIAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS social_events (
 agent_id TEXT NOT NULL, social_id TEXT NOT NULL, payload TEXT NOT NULL,
 created_at REAL NOT NULL, expires_at REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1,
 PRIMARY KEY(agent_id,social_id));
CREATE TABLE IF NOT EXISTS role_states (
 agent_id TEXT NOT NULL, state_id TEXT NOT NULL, social_id TEXT NOT NULL,
 payload TEXT NOT NULL, version INTEGER NOT NULL, status TEXT NOT NULL,
 created_at REAL NOT NULL, expires_at REAL NOT NULL, next_at REAL NOT NULL,
 followups INTEGER NOT NULL DEFAULT 0, public_allowed INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(agent_id,state_id));
CREATE INDEX IF NOT EXISTS role_states_active ON role_states(agent_id,status,expires_at,next_at);
"""


class SQLiteSocial:
    def _social_source_valid(self, db, source, now):
        event_id = source.get("event_id")
        if not event_id and not source.get("owner_activity_version"):
            return False
        if event_id:
            row = db.execute("SELECT payload,state FROM events WHERE agent_id=? AND event_id=?", (self.agent_id,event_id)).fetchone()
            if not row or row["state"] in {"deleted", "denied", "baseline"}:
                return False
            if db.execute("SELECT 1 FROM memory_suppressed WHERE agent_id=? AND event_id=?", (self.agent_id,event_id)).fetchone():
                return False
            event = json.loads(row["payload"])
            if any(event.get(k) != source.get(k) for k in ("platform","account_id","actor_id","conversation_id","visibility")):
                return False
        for person_key, platform_key, version_key in (("person_id","platform","binding_version"),
                                                       ("subject_person_id","owner_binding_platform","owner_binding_version")):
            if source.get(version_key):
                row = db.execute("SELECT version FROM bindings WHERE agent_id=? AND person_id=? AND platform=? AND active=1",
                                 (self.agent_id,source[person_key],source[platform_key])).fetchone()
                if not row or row[0] != source[version_key]:
                    return False
        if source.get("owner_activity_version"):
            anchor = source["activity_anchor"]
            binding = db.execute("SELECT version FROM bindings WHERE agent_id=? AND person_id=? AND platform=? AND active=1",
                                 (self.agent_id,source["person_id"],source["activity_platform"])).fetchone()
            if not binding or binding[0] != anchor["binding_version"]:
                return False
            row = db.execute("SELECT value FROM settings WHERE agent_id=? AND key=?", (self.agent_id,"owner_history:"+source["person_id"])).fetchone()
            current = json.loads(row[0]) if row else {}
            if any(current.get(k) != anchor.get(k) for k in ("revision","auth")):
                return False
            for rid in source["owner_activity_sources"]:
                if not db.execute("SELECT 1 FROM owner_history WHERE agent_id=? AND person_id=? AND record_id=? AND eligible=1 AND expires_at>?",
                                  (self.agent_id,source["person_id"],rid,now)).fetchone():
                    return False
        return True

    def _social_cancel(self, db, ids, *, scrub=False):
        if not ids:
            return
        marks = ",".join("?" for _ in ids)
        db.execute(f"UPDATE actions SET state=CASE WHEN state IN ('draft','queued','reviewing') THEN 'cancelled' ELSE state END "
                   + (",payload=json_set(payload,'$.text','[社交来源已遗忘]') " if scrub else " ") +
                   f"WHERE agent_id=? AND EXISTS (SELECT 1 FROM json_each(actions.payload,'$.social_refs') r WHERE json_extract(r.value,'$.id') IN ({marks}))",
                   (self.agent_id,*ids))

    def _social_forget_source(self, db, social_id):
        ids = [r[0] for r in db.execute("SELECT state_id FROM role_states WHERE agent_id=? AND social_id=?", (self.agent_id,social_id))]
        db.execute("UPDATE social_events SET active=0,payload=json_set(payload,'$.quote','','$.summary','','$.url','') WHERE agent_id=? AND social_id=?", (self.agent_id,social_id))
        db.execute("UPDATE role_states SET status='forgotten',version=version+1,payload=json_set(payload,'$.note','') WHERE agent_id=? AND social_id=? AND status!='forgotten'", (self.agent_id,social_id))
        self._social_cancel(db,ids,scrub=True)

    def _social_prune(self, db, now):
        rows = db.execute("SELECT social_id,payload,expires_at FROM social_events WHERE agent_id=? AND active=1", (self.agent_id,)).fetchall()
        for row in rows:
            if row["expires_at"] <= now or not self._social_source_valid(db,json.loads(row["payload"]),now):
                self._social_forget_source(db,row["social_id"])
        expired = [r[0] for r in db.execute("SELECT state_id FROM role_states WHERE agent_id=? AND status='active' AND expires_at<=?", (self.agent_id,now))]
        db.execute("UPDATE role_states SET status='expired',version=version+1,payload=json_set(payload,'$.note','') WHERE agent_id=? AND status='active' AND expires_at<=?", (self.agent_id,now))
        self._social_cancel(db,expired)

    def social_commit(self, source_id, source, state, candidates, resolve, create, public_allowed,
                      now, ttl_hours, retention_days, max_active, delay, epoch, owner):
        empty = {"id":"", "resolved":[]}
        with self.transaction() as db:
            self._social_prune(db,now)
            if epoch != self._memory_epoch(db) or not self._social_source_valid(db,source,now):
                return empty
            if db.execute("SELECT 1 FROM social_events WHERE agent_id=? AND social_id=?", (self.agent_id,source_id)).fetchone():
                old = db.execute("SELECT state_id,public_allowed FROM role_states WHERE agent_id=? AND social_id=? AND status='active'", (self.agent_id,source_id)).fetchone()
                return {"id":old[0],"resolved":[],"public_allowed":bool(old[1])} if old else empty
            # Mark even a resolution-only event as consumed, so retries cannot
            # resolve a newer state twice or revive an old feeling.
            db.execute("INSERT INTO social_events VALUES(?,?,?,?,?,1)",
                       (self.agent_id,source_id,json.dumps(source,ensure_ascii=False),now,now+retention_days*86400))
            resolved = []
            for sid in dict.fromkeys(resolve):
                row = db.execute("SELECT r.version,r.status,e.payload FROM role_states r JOIN social_events e ON e.agent_id=r.agent_id AND e.social_id=r.social_id WHERE r.agent_id=? AND r.state_id=?", (self.agent_id,sid)).fetchone()
                if not row or row["status"] != "active" or row["version"] != candidates[sid]:
                    continue
                old = json.loads(row["payload"])
                if source["occurred_at"] < old["occurred_at"]:
                    continue
                permitted = owner and old["subject_person_id"] == source["person_id"] or (
                    old["person_id"] == source["person_id"] and old["conversation_id"] == source["conversation_id"] and
                    old["platform"] == source["platform"])
                if permitted:
                    db.execute("UPDATE role_states SET status='resolved',version=version+1 WHERE agent_id=? AND state_id=?", (self.agent_id,sid))
                    resolved.append(sid)
            self._social_cancel(db,resolved)
            if not create or db.execute("SELECT COUNT(*) FROM role_states WHERE agent_id=? AND status='active'",(self.agent_id,)).fetchone()[0] >= max_active:
                return {"id":"", "resolved":resolved}
            sid = stable_id(self.agent_id,"role-state",source_id)
            db.execute("INSERT INTO role_states VALUES(?,?,?,?,1,'active',?,?,?,0,?)",
                       (self.agent_id,sid,source_id,json.dumps(state,ensure_ascii=False),now,now+ttl_hours*3600,now+delay*60,int(public_allowed)))
            return {"id":sid,"resolved":resolved,"public_allowed":public_allowed}

    def social_rows(self, now, *, limit=100, include_inactive=False):
        with self.transaction() as db:
            self._social_prune(db,now)
            where = "" if include_inactive else " AND r.status='active' AND e.active=1"
            rows = db.execute("SELECT r.*,e.payload AS source FROM role_states r JOIN social_events e ON e.agent_id=r.agent_id AND e.social_id=r.social_id WHERE r.agent_id=?"+where+" ORDER BY r.created_at DESC,r.state_id LIMIT ?", (self.agent_id,min(100,limit))).fetchall()
            return [{**dict(r),"state":json.loads(r["payload"]),"source":json.loads(r["source"])} for r in rows]

    def social_pending(self, state_id):
        with self.connection() as db:
            return bool(db.execute("SELECT 1 FROM actions a,json_each(a.payload,'$.social_followups') s WHERE a.agent_id=? AND s.value=? AND a.state IN ('draft','queued','reviewing','sending','unknown') LIMIT 1", (self.agent_id,state_id)).fetchone())

    def _social_validate(self, db, action, now):
        if any(sid not in {r.get("id") for r in action.social_refs} for sid in action.social_followups):
            return "social_followup_reference_missing"
        for ref in action.social_refs:
            if (not isinstance(ref,dict) or set(ref) != {"id","version","projection"} or
                    type(ref["version"]) is not int or not isinstance(ref["id"],str) or
                    ref["projection"] not in {"current","private","public_event","public_feeling"}):
                return "social_reference_invalid"
            row = db.execute("SELECT r.*,e.payload AS source,e.active AS source_active,e.expires_at AS source_expiry FROM role_states r JOIN social_events e ON e.agent_id=r.agent_id AND e.social_id=r.social_id WHERE r.agent_id=? AND r.state_id=?", (self.agent_id,ref["id"])).fetchone()
            if (not row or row["status"] != "active" or row["version"] != ref["version"] or row["expires_at"] <= now or
                    not row["source_active"] or row["source_expiry"] <= now):
                return "social_state_invalidated"
            source = json.loads(row["source"])
            if not self._social_source_valid(db,source,now):
                return "social_source_invalidated"
            if action.visibility == "public":
                if not row["public_allowed"] or source["visibility"] != "public" and ref["projection"] != "public_feeling":
                    return "social_privacy_denied"
            elif source["visibility"] != "public":
                target = db.execute("SELECT person_id FROM bindings WHERE agent_id=? AND platform=? AND external_id=? AND active=1", (self.agent_id,action.platform,action.recipient_id)).fetchone()
                if (not target or target[0] != source["person_id"]) and not (
                        action.platform == source["platform"] and action.recipient_id == source["actor_id"]):
                    return "social_private_route_denied"
        return ""

    def social_validate(self, action, now):
        with self.connection() as db:
            return self._social_validate(db,action,now)

    def social_sources(self, refs):
        with self.connection() as db:
            rows = []
            for ref in refs:
                row = db.execute("SELECT e.payload FROM role_states r JOIN social_events e ON e.agent_id=r.agent_id AND e.social_id=r.social_id WHERE r.agent_id=? AND r.state_id=?", (self.agent_id,ref["id"])).fetchone()
                if row:
                    rows.append(json.loads(row[0]))
            return rows

    def social_action_context(self, action):
        # Used only after guard(); reapply the exact action's projection rather
        # than handing the reviewer raw private provenance for a public post.
        with self.connection() as db:
            states = []
            for ref in action.social_refs:
                row = db.execute("SELECT r.payload,e.payload AS source FROM role_states r JOIN social_events e ON e.agent_id=r.agent_id AND e.social_id=r.social_id WHERE r.agent_id=? AND r.state_id=?", (self.agent_id,ref["id"])).fetchone()
                state, source = json.loads(row["payload"]),json.loads(row["source"])
                item = {"id":ref["id"],"feeling":state["feeling"],"projection":ref["projection"]}
                if action.visibility == "public" and source["visibility"] != "public":
                    item["certainty"] = "feeling_only"
                else:
                    item.update(summary=source["summary"],quote=source["quote"],certainty=source["certainty"])
                states.append(item)
            return {"states":states}

    def social_forget(self, state_id, *, reason="forgotten", resolve=False):
        with self.transaction() as db:
            row = db.execute("SELECT social_id FROM role_states WHERE agent_id=? AND state_id=?", (self.agent_id,state_id)).fetchone()
            if not row:
                raise ValueError("社交状态不存在")
            if resolve:
                db.execute("UPDATE role_states SET status='resolved',version=version+1 WHERE agent_id=? AND state_id=? AND status='active'", (self.agent_id,state_id))
                self._social_cancel(db,[state_id])
            else:
                self._social_forget_source(db,row[0])

    def _social_accepted(self, db, action, now, gap_seconds):
        for sid in action.social_followups:
            db.execute("UPDATE role_states SET followups=followups+1,next_at=MAX(next_at,?) WHERE agent_id=? AND state_id=? AND status='active'", (now+gap_seconds,self.agent_id,sid))

    def social_invalidate_events(self, db, event_ids):
        for event_id in event_ids:
            rows = db.execute("SELECT social_id FROM social_events WHERE agent_id=? AND active=1 AND json_extract(payload,'$.event_id')=?", (self.agent_id,event_id)).fetchall()
            for row in rows:
                self._social_forget_source(db,row[0])
