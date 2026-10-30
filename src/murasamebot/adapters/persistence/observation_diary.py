"""Private observation journals with expiring provenance, separate from dialogue."""
import json

from murasamebot.domain.models import stable_id


OBSERVATION_DIARY_SCHEMA = """
CREATE TABLE IF NOT EXISTS observation_diary_sources (
 agent_id TEXT NOT NULL, person_id TEXT NOT NULL, platform TEXT NOT NULL,
 product_id TEXT NOT NULL, record_id TEXT NOT NULL, auth TEXT NOT NULL,
 revision TEXT NOT NULL, binding_version INTEGER NOT NULL,
 PRIMARY KEY(agent_id,product_id,record_id));
CREATE INDEX IF NOT EXISTS observation_diary_coverage
 ON observation_diary_sources(agent_id,person_id,record_id);
"""


class SQLiteObservationDiary:
    def _invalidate_observation_diaries(self, db, now, *, person_id=None, platform=None):
        where = "s.agent_id=?"
        args = [self.agent_id]
        if person_id is not None:
            where += " AND s.person_id=?"
            args.append(person_id)
        if platform is not None:
            where += " AND s.platform=?"
            args.append(platform)
        else:
            where += " AND (h.record_id IS NULL OR h.expires_at<=? OR b.active IS NOT 1 OR b.version!=s.binding_version OR json_extract(st.value,'$.auth') IS NOT s.auth OR json_extract(st.value,'$.revision') IS NOT s.revision)"
            args.append(now)
        db.execute("UPDATE memory_products SET active=0,text='[观察来源已失效]' WHERE agent_id=? AND product_id IN ("
                   "SELECT s.product_id FROM observation_diary_sources s LEFT JOIN owner_history h ON h.agent_id=s.agent_id AND h.person_id=s.person_id AND h.record_id=s.record_id "
                   "LEFT JOIN bindings b ON b.agent_id=s.agent_id AND b.person_id=s.person_id AND b.platform=s.platform "
                   "LEFT JOIN settings st ON st.agent_id=s.agent_id AND st.key='owner_history:'||s.person_id WHERE "+where+")",
                   (self.agent_id,*args))

    def observation_diary_batch(self, person_id, now, cutoff, retention_days, limit):
        with self.connection() as db:
            rows = db.execute("SELECT h.record_id,h.payload FROM owner_history h WHERE h.agent_id=? AND h.person_id=? AND h.eligible=1 AND h.expires_at>? AND h.viewed_at>? AND h.viewed_at<=? "
                              "AND NOT EXISTS (SELECT 1 FROM observation_diary_sources s WHERE s.agent_id=h.agent_id AND s.person_id=h.person_id AND s.record_id=h.record_id) ORDER BY h.viewed_at,h.record_id LIMIT ?",
                              (self.agent_id,person_id,now,now-retention_days*86400,cutoff,min(20,limit))).fetchall()
        return [{"record_id":r["record_id"],**json.loads(r["payload"])} for r in rows]

    def observation_diary_commit(self, context, text, epoch, now, retention_days):
        with self.transaction() as db:
            if epoch != self._memory_epoch(db):
                return False
            person, platform = context["person_id"],context["platform"]
            state = db.execute("SELECT value FROM settings WHERE agent_id=? AND key=?",(self.agent_id,"owner_history:"+person)).fetchone()
            state = json.loads(state[0]) if state else {}
            binding = db.execute("SELECT version,external_id FROM bindings WHERE agent_id=? AND person_id=? AND platform=? AND active=1",(self.agent_id,person,platform)).fetchone()
            if (not binding or binding["version"]!=context["binding_version"] or binding["external_id"]!=context["external_id"]
                    or state.get("revision")!=context["revision"] or state.get("auth")!=context["auth"]):
                return False
            ids = [r["record_id"] for r in context["records"]]
            if not ids:
                return False
            for rid in ids:
                valid = db.execute("SELECT 1 FROM owner_history WHERE agent_id=? AND person_id=? AND record_id=? AND eligible=1 AND expires_at>? AND viewed_at>?",(self.agent_id,person,rid,now,now-retention_days*86400)).fetchone()
                covered = db.execute("SELECT 1 FROM observation_diary_sources WHERE agent_id=? AND person_id=? AND record_id=?",(self.agent_id,person,rid)).fetchone()
                if not valid or covered:
                    return False
            pid = stable_id(self.agent_id,"observation_diary",context["revision"],ids,text,epoch)
            db.execute("INSERT INTO memory_products VALUES(?,?,?,?,?,?,?,?,1,0,'')",(self.agent_id,pid,person,"observation:"+platform,"owner_observation_diary","diary",text,now))
            db.executemany("INSERT INTO observation_diary_sources VALUES(?,?,?,?,?,?,?,?)",
                           [(self.agent_id,person,platform,pid,rid,context["auth"],context["revision"],binding["version"]) for rid in ids])
            return True

    def memory_feed(self, kind, limit=50, *, platform=""):
        if kind not in {"memory","diary","reflection"}:
            raise ValueError("未知记忆板块")
        where = "kind NOT IN ('diary','reflection')" if kind=="memory" else "kind=?"
        params = (self.agent_id,) if kind=="memory" else (self.agent_id,kind)
        platform_where = ""
        if platform:
            platform_where = """ AND (EXISTS (SELECT 1 FROM memory_sources s JOIN conversation_records r
                ON r.agent_id=s.agent_id AND r.record_id=s.record_id WHERE s.agent_id=memory_products.agent_id
                AND s.product_id=memory_products.product_id AND r.active=1 AND r.platform=?)
                OR (?='bilibili' AND purpose='owner_observation_diary'))"""
            params += (platform,platform)
        with self.connection() as db:
            rows = db.execute(f"SELECT * FROM memory_products WHERE agent_id=? AND active=1 AND {where}{platform_where} ORDER BY created_at DESC,product_id LIMIT ?",(*params,min(100,limit))).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                sources = db.execute("""SELECT DISTINCT r.platform FROM memory_sources s JOIN conversation_records r
                    ON r.agent_id=s.agent_id AND r.record_id=s.record_id
                    WHERE s.agent_id=? AND s.product_id=? AND r.active=1 ORDER BY r.platform""",(self.agent_id,item['product_id'])).fetchall()
                item['platforms'] = [r[0] for r in sources]
                if item['purpose']=='owner_observation_diary':
                    item['platforms'] = ['bilibili']
                result.append(item)
        return result
