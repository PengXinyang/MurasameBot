"""Durable debounce inbox. Commit the batch event and consumption atomically."""
import json
from dataclasses import replace

from murasamebot.domain.models import Event, stable_id


class ChatBatches:
    def __init__(self, store):
        self.store = store
        with store.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS chat_inbox (
                agent TEXT, id TEXT, peer TEXT, account TEXT, received REAL, version INTEGER,
                payload TEXT, state TEXT, PRIMARY KEY(agent,id))""")
            if "epoch" not in {r[1] for r in db.execute("PRAGMA table_info(chat_inbox)")}:
                db.execute("ALTER TABLE chat_inbox ADD COLUMN epoch INTEGER NOT NULL DEFAULT -1")

    def receive(self, event, binding, now):
        with self.store.transaction() as db:
            epoch = db.execute("SELECT value FROM settings WHERE agent_id=? AND key='memory_epoch'", (self.store.agent_id,)).fetchone()
            return db.execute("INSERT OR IGNORE INTO chat_inbox(agent,id,peer,account,received,version,payload,state,epoch) VALUES(?,?,?,?,?,?,?,'pending',?)",
                (self.store.agent_id, stable_id(event.platform,event.account_id,event.event_id), event.actor_id,
                 event.account_id, now, binding["version"], json.dumps(event.to_dict(),ensure_ascii=False), int(json.loads(epoch[0])) if epoch else 0)).rowcount == 1

    def flush(self, settings, owner, normalize, now):
        binding = self.store.binding(owner, "qq")
        with self.store.transaction() as db:
            rows = db.execute("SELECT * FROM chat_inbox WHERE agent=? AND state='pending' ORDER BY received,rowid", (self.store.agent_id,)).fetchall()
            active = db.execute("SELECT external_id,version FROM bindings WHERE agent_id=? AND person_id=? AND platform='qq' AND active=1", (self.store.agent_id,owner)).fetchone()
            epoch_row = db.execute("SELECT value FROM settings WHERE agent_id=? AND key='memory_epoch'", (self.store.agent_id,)).fetchone()
            epoch = int(json.loads(epoch_row[0])) if epoch_row else 0
            valid = [r for r in rows if r["epoch"] == epoch and active and r["peer"]==active[0] and r["version"]==active[1] and r["account"]==settings["robot_id"] and now-r["received"]<86400]
            for row in rows:
                if row not in valid:
                    db.execute("UPDATE chat_inbox SET state='discarded',payload='' WHERE agent=? AND id=?", (self.store.agent_id,row["id"]))
            if not valid:
                return False
            selected, characters, image_count = [], 0, 0
            for row in valid[:20]:
                item = Event.from_dict(json.loads(row["payload"]))
                if selected and (characters + len(item.text)>12000 or image_count + len(item.images)>9):
                    break
                selected.append(row)
                characters += len(item.text)
                image_count += len(item.images)
            if (len(selected)==len(valid) and len(valid)<20 and now-valid[-1]["received"]<settings["quiet_seconds"]
                    and now-valid[0]["received"]<settings["max_wait_seconds"]):
                return False
            events = [Event.from_dict(json.loads(r["payload"])) for r in selected]
            text = "\n".join(f"第{i+1}条：{e.text}" for i,e in enumerate(events))
            images = tuple(replace(image,index=i+1) for i,image in enumerate(image for e in events for image in e.images))
            event = normalize(replace(events[-1],event_id=stable_id("batch",[r["id"] for r in selected]),
                text=text,images=images,occurred_at=now))
            # Tombstones stay authoritative; raw IDs remain as content-free dedup entries.
            suppressed = db.execute("SELECT 1 FROM memory_suppressed WHERE agent_id=? AND event_id=?", (self.store.agent_id,event.event_id)).fetchone()
            if not suppressed:
                db.execute("INSERT OR IGNORE INTO events(agent_id,event_id,payload,state) VALUES(?,?,?,'pending')", (self.store.agent_id,event.event_id,json.dumps(event.to_dict(),ensure_ascii=False)))
            for row in selected:
                db.execute("UPDATE chat_inbox SET state='flushed',payload='' WHERE agent=? AND id=?",(self.store.agent_id,row["id"]))
            return True

    def activity(self, account, peer, version, now):
        with self.store.connection() as db:
            row = db.execute("""SELECT MAX(received),COUNT(CASE WHEN state='pending' THEN 1 END)
                FROM chat_inbox WHERE agent=? AND account=? AND peer=? AND version=? AND epoch=?
                AND state IN ('pending','flushed') AND received>?""",
                (self.store.agent_id,account,peer,version,self.store.get_setting("memory_epoch",0),now-86400)).fetchone()
            return {"last_at":row[0] or 0,"pending":row[1]}
