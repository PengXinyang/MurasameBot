import json


REQUEST_SCHEMA = """
CREATE TABLE IF NOT EXISTS platform_requests (
 id INTEGER PRIMARY KEY, agent_id TEXT NOT NULL, role TEXT NOT NULL, channel TEXT NOT NULL,
 method TEXT NOT NULL, host TEXT NOT NULL, path TEXT NOT NULL, created_at REAL NOT NULL,
 duration_ms INTEGER NOT NULL, http_status INTEGER, api_code TEXT, error TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS platform_requests_time ON platform_requests(agent_id,created_at);
"""


class SQLiteRequests:
    def claim_poll_interval(self, channel, now, interval):
        with self.transaction() as db:
            key="bili_poll_at:"+channel
            if now-self._request_state(db,key,0)<interval:return False
            self._request_save(db,key,now)
            return True

    def defer_action(self, action_id, retry_at, reason):
        with self.transaction() as db:
            row=db.execute("SELECT payload FROM actions WHERE agent_id=? AND action_id=? AND state='sending'",(self.agent_id,action_id)).fetchone()
            if not row:return
            payload=json.loads(row[0]);payload["retry_at"]=retry_at
            db.execute("UPDATE actions SET state='queued',payload=?,lease_until=0,sent_at=NULL,receipt=? WHERE agent_id=? AND action_id=?",
                       (json.dumps(payload,ensure_ascii=False),json.dumps({"state":"deferred","detail":reason}),self.agent_id,action_id))

    def _request_state(self, db, key, default=None):
        row=db.execute("SELECT value FROM settings WHERE agent_id=? AND key=?",(self.agent_id,key)).fetchone()
        return json.loads(row[0]) if row else default

    def _request_save(self, db, key, value):
        db.execute("INSERT INTO settings VALUES(?,?,?) ON CONFLICT(agent_id,key) DO UPDATE SET value=excluded.value",
                   (self.agent_id,key,json.dumps(value)))

    def request_slot(self, now, gap, max_wait):
        with self.transaction() as db:
            due=max(now,self._request_state(db,"bili_read_slot",0))
            if due-now>max_wait:
                return None
            self._request_save(db,"bili_read_slot",due+gap)
            return due

    def request_cooldown(self, channel):
        return self.get_setting("bili_backoff:"+channel,{})

    def request_result(self, record, *, limited, base, maximum, retry_after, retention, max_rows):
        with self.transaction() as db:
            key="bili_backoff:"+record["channel"]
            state=self._request_state(db,key,{})
            if limited:
                failures=min(16,state.get("failures",0)+1)
                delay=max(min(maximum,base*2**(failures-1)),retry_after)
                self._request_save(db,key,{"failures":failures,"until":record["created_at"]+delay,
                    "at":record["created_at"],"reason":str(record.get("api_code") or record.get("http_status")),
                    "path":record["path"],"role":record["role"]})
            elif record.get("http_status")==200 and record.get("api_code")=="0":
                if state and record.get("started_at",record["created_at"])>=state.get("until",0):
                    self._request_save(db,key,{})
            db.execute("INSERT INTO platform_requests(agent_id,role,channel,method,host,path,created_at,duration_ms,http_status,api_code,error) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (self.agent_id,*[record[k] for k in ("role","channel","method","host","path","created_at","duration_ms","http_status","api_code","error")]))
            db.execute("DELETE FROM platform_requests WHERE agent_id=? AND created_at<?",(self.agent_id,record["created_at"]-retention*86400))
            db.execute("DELETE FROM platform_requests WHERE agent_id=? AND id NOT IN (SELECT id FROM platform_requests WHERE agent_id=? ORDER BY id DESC LIMIT ?)",
                       (self.agent_id,self.agent_id,max_rows))

    def polling_quiet(self, now, before, after, *, platform=None):
        if before==0 and after==0:
            return {}
        with self.connection() as db:
            scope = " AND json_extract(payload,'$.platform')=?" if platform is not None else ""
            rows=db.execute("SELECT action_id,state,sent_at,payload,expires_at FROM actions WHERE agent_id=? AND kind IN ('autonomous_post','proactive_private') AND (state IN ('queued','sending') OR (state IN ('accepted','unknown') AND sent_at>=?))" + scope,
                            (self.agent_id,now-after,*((platform,) if platform is not None else ()))).fetchall()
        for row in rows:
            action=json.loads(row["payload"])
            planned=action.get("not_before",0)
            due=row["sent_at"] if row["state"] in {"accepted","unknown"} else planned
            start=due-after if row["state"] in {"accepted","unknown"} else due-before
            end=due+after
            if due and row["expires_at"]>now and start<=now<end:
                return {"action_id":row["action_id"],"planned_at":planned,"until":end}
        return {}

    def request_status(self, now, settings):
        with self.connection() as db:
            rows=db.execute("SELECT role,channel,method,host,path,COUNT(*) count,MAX(duration_ms) max_ms FROM platform_requests WHERE agent_id=? AND created_at>=? GROUP BY role,channel,method,host,path ORDER BY count DESC",(self.agent_id,now-60)).fetchall()
            recent=db.execute("SELECT role,channel,method,host,path,created_at,duration_ms,http_status,api_code,error FROM platform_requests WHERE agent_id=? ORDER BY id DESC LIMIT 30",(self.agent_id,)).fetchall()
            states=db.execute("SELECT key,value FROM settings WHERE agent_id=? AND key LIKE 'bili_backoff:%'",(self.agent_id,)).fetchall()
        return {"last_minute":sum(r["count"] for r in rows),"endpoints":[dict(r) for r in rows],"recent":[dict(r) for r in recent],
                "platform_paused":self.get_setting("platform_paused:bilibili",False),"pause_reason":self.get_setting("platform_pause_reason:bilibili",{}),
                "cooldowns":[{"channel":r["key"].split(":",1)[1],**json.loads(r["value"])} for r in states if json.loads(r["value"]).get("until",0)>now],
                "quiet":self.polling_quiet(now,settings.quiet_before_seconds,settings.quiet_after_seconds)}
