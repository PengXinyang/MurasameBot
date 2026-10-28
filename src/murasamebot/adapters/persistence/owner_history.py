import json
from uuid import uuid4

from murasamebot.domain.models import stable_id


HISTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS owner_history (
 agent_id TEXT NOT NULL, person_id TEXT NOT NULL, record_id TEXT NOT NULL,
 viewed_at REAL NOT NULL, expires_at REAL NOT NULL, eligible INTEGER NOT NULL,
 payload TEXT NOT NULL, PRIMARY KEY(agent_id,person_id,record_id));
"""


class SQLiteOwnerHistory:
    def owner_history_prune(self, person_id, now, retention_days):
        with self.connection() as db:
            db.execute("DELETE FROM owner_history WHERE agent_id=? AND person_id=? AND (viewed_at<? OR expires_at<=?)",
                       (self.agent_id,person_id,now-retention_days*86400,now))

    def owner_history_state(self, person_id):
        return self.get_setting("owner_history:" + person_id, {"revision": "initial", "state": "not_synced"})

    def owner_history_status(self, person_id, revision, patch):
        key = "owner_history:" + person_id
        with self.transaction() as db:
            row = db.execute("SELECT value FROM settings WHERE agent_id=? AND key=?", (self.agent_id,key)).fetchone()
            state = json.loads(row[0]) if row else {"revision":"initial", "state":"not_synced"}
            if state["revision"] != revision:
                return False
            state.update(patch)
            db.execute("INSERT INTO settings VALUES(?,?,?) ON CONFLICT(agent_id,key) DO UPDATE SET value=excluded.value",
                       (self.agent_id,key,json.dumps(state)))
            return True

    def owner_history_reset(self, person_id, now, *, forget=True):
        key = "owner_history:" + person_id
        with self.transaction() as db:
            db.execute("DELETE FROM owner_history WHERE agent_id=? AND person_id=?", (self.agent_id,person_id))
            self._bump_memory_epoch(db)
            state = {"revision":uuid4().hex,"state":"not_synced","baseline_at":now,"forget_before":now if forget else 0}
            db.execute("INSERT INTO settings VALUES(?,?,?) ON CONFLICT(agent_id,key) DO UPDATE SET value=excluded.value",
                       (self.agent_id,key,json.dumps(state)))
            # Forgetting observations invalidates already composed private plans.
            db.execute("UPDATE actions SET state='cancelled' WHERE agent_id=? AND state IN ('draft','queued','reviewing') AND COALESCE(json_extract(payload,'$.owner_activity_version'),'')<>''", (self.agent_id,))

    def owner_history_commit(self, person_id, binding, revision, auth, rows, warning, now, retention_days):
        key = "owner_history:" + person_id
        with self.transaction() as db:
            saved = db.execute("SELECT value FROM settings WHERE agent_id=? AND key=?", (self.agent_id,key)).fetchone()
            state = json.loads(saved[0]) if saved else {"revision":"initial","state":"not_synced"}
            current = self.binding(person_id,binding["platform"])
            if state["revision"] != revision or not current or current["version"] != binding["version"] or current["external_id"] != binding["external_id"]:
                return False
            first = not state.get("initialized") or state.get("auth") != auth
            baseline = now if first else state.get("baseline_at", now)
            db.execute("DELETE FROM owner_history WHERE agent_id=? AND person_id=? AND expires_at<=?", (self.agent_id,person_id,now))
            if first:
                db.execute("DELETE FROM owner_history WHERE agent_id=? AND person_id=?", (self.agent_id,person_id))
            for item in rows:
                viewed = item["viewed_at"]
                if viewed < now-retention_days*86400 or viewed > now+60 or viewed<=state.get("forget_before",0):
                    continue
                rid = stable_id(item["content_id"],viewed)
                eligible = not first and viewed > baseline
                db.execute("INSERT INTO owner_history VALUES(?,?,?,?,?,?,?) ON CONFLICT(agent_id,person_id,record_id) DO UPDATE SET payload=excluded.payload",
                           (self.agent_id,person_id,rid,viewed,viewed+retention_days*86400,int(eligible),json.dumps(item,ensure_ascii=False)))
            state.update(initialized=True, baseline_at=baseline, auth=auth, last_success=now,
                         state="baseline" if first else "synced", error="", warning=warning,
                         watermark=max([state.get("watermark",0), *[r["viewed_at"] for r in rows]]))
            db.execute("INSERT INTO settings VALUES(?,?,?) ON CONFLICT(agent_id,key) DO UPDATE SET value=excluded.value", (self.agent_id,key,json.dumps(state)))
            return True

    def owner_history_rows(self, person_id, now, *, limit=30, since=0, eligible_only=False):
        with self.connection() as db:
            rows = db.execute("SELECT payload FROM owner_history WHERE agent_id=? AND person_id=? AND expires_at>? AND viewed_at>=? AND eligible>=? ORDER BY viewed_at DESC,record_id LIMIT ?",
                              (self.agent_id,person_id,now,since,int(eligible_only),min(90,limit))).fetchall()
            return [json.loads(row[0]) for row in rows]
