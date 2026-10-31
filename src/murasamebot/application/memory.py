"""Platform-neutral retrieval, context budgeting and background consolidation."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, time as daytime
import json
import re
import time
from zoneinfo import ZoneInfo

from murasamebot.domain.models import ModelError, PolicyError, stable_id
from murasamebot.ports import MemoryRepository, ModelGateway


@dataclass(frozen=True)
class MemorySettings:
    enabled: bool = True
    recent_messages: int = 20
    recall_limit: int = 8
    context_characters: int = 12000
    summary_messages: int = 40
    batch_messages: int = 80
    input_characters: int = 24000
    pressure_ratio: float = 0.75
    review_time: str = "23:00"
    daily_call_limit: int = 10
    retry_minutes: int = 10


class MemoryService:
    def __init__(self, store: MemoryRepository, model: ModelGateway, settings: MemorySettings | None = None,
                 *, timezone="Asia/Hong_Kong", observations=None, persona="", observation_guard=None):
        self.shared_owner_private = False
        self.store, self.model = store, model
        self.settings = settings or MemorySettings()
        self.timezone = timezone
        self.observations, self.persona = observations, persona
        self.observation_guard = observation_guard

    def public_initiative_context(self, now):
        epoch = self.store.get_setting("memory_epoch",0)
        history, memories = [], []
        if self.settings.enabled:
            for scope in self.store.memory_scopes():
                if scope["purpose"] != "public_dialogue":
                    continue
                rows = self.store.memory_records(scope["person_id"],scope["scope"],"public_dialogue",
                    limit=self.settings.recent_messages,before=now)
                history.extend({"speaker":r["role"],"text":r["text"],"time":r["occurred_at"],"source":r["record_id"]} for r in rows if r["visibility"]=="public")
                for product in self.store.memory_products(scope["person_id"],scope["scope"],"public_dialogue",limit=10):
                    sources=self.store.memory_source_records(list(product["sources"]))
                    if sources and len(sources)==len(set(product["sources"])) and all(
                            r["active"] and r["visibility"]=="public" and r["purpose"]=="public_dialogue" for r in sources):
                        memories.append({"text":product["text"],"kind":product["kind"],"sources":product["sources"]})
        history.sort(key=lambda r:r["time"],reverse=True)
        remaining = self.settings.context_characters
        selected = {"history":[],"memories":[],"memory_epoch":epoch}
        for key,items in (("history",history[:self.settings.recent_messages]),("memories",memories)):
            for item in items:
                cost=len(json.dumps(item,ensure_ascii=False))
                if cost<=remaining:
                    selected[key].append(item);remaining-=cost
        selected["history"].reverse()
        return selected

    def context(self, event, person, owner, now):
        epoch = self.store.get_setting("memory_epoch", 0)
        if not self.settings.enabled:
            return {"history": [], "memories": [], "memory_epoch": epoch}
        person = person or f"unlinked:{event.platform}:{event.actor_id}"
        scope = f"conversation:{event.platform}:{event.conversation_id}"
        purpose = "private_dialogue" if event.visibility == "private" else "public_dialogue"
        history = self.store.memory_records(person,scope,purpose,limit=self.settings.recent_messages,before=event.occurred_at)
        shared_products = []
        if owner and event.visibility == "private":
            # The same agent remembers its public posts while speaking privately.
            for other in self.store.memory_scopes():
                if other["person_id"]=="agent:self" and other["purpose"]=="public_dialogue":
                    history += self.store.memory_records("agent:self",other["scope"],"public_dialogue",
                        limit=self.settings.recent_messages,before=event.occurred_at)
            history = sorted(history,key=lambda r:r["occurred_at"],reverse=True)[:self.settings.recent_messages]
        if owner and event.visibility == "private" and self.shared_owner_private:
            for other in self.store.memory_scopes():
                binding = self.store.binding(person, other["platform"])
                if (other["person_id"] == person and other["purpose"] == "private_dialogue"
                        and other["scope"] != scope and other["platform"] in {"qq","bilibili"} and binding):
                    history += self.store.memory_records(person,other["scope"],"private_dialogue",limit=self.settings.recent_messages,before=event.occurred_at)
                    shared_products += self.store.memory_products(person,other["scope"],"private_dialogue",limit=20)
            history = sorted(history,key=lambda r:r["occurred_at"],reverse=True)[:self.settings.recent_messages]
        seen = {r["record_id"] for r in history}
        recall = self.store.memory_records(person,scope,purpose,query=event.text,limit=self.settings.recall_limit,before=event.occurred_at)
        products = shared_products + self.store.memory_products(person,scope,purpose,limit=40)
        products += self.store.memory_products(person,scope,purpose,limit=40,query=event.text)
        products += self.store.memory_corrections([r["record_id"] for r in history+recall])
        products = list({p["product_id"]:p for p in products}.values())
        # Corrections supersede interpretations of the same historical sources.
        corrected = {rid for p in products if p["kind"]=="correction" for rid in p["sources"]}
        history = [r for r in history if r["record_id"] not in corrected]
        recall = [r for r in recall if r["record_id"] not in corrected]
        ranked = sorted(products,key=lambda p:(p["pinned"],p["kind"]=="correction",self._score(p["text"],event.text),p["kind"]=="fact",p["created_at"]),reverse=True)
        memories = []
        # Only explicit purpose-filtered preference records may cross channels.
        legacy_purpose = "owner_dialogue" if owner and event.visibility=="private" else purpose
        legacy = self.store.memory(person,scope,legacy_purpose,now,limit=20)
        legacy += self.store.memory(person,scope,legacy_purpose,now,limit=30,query=event.text)
        memories += list({m["memory_id"]:m for m in legacy}.values())
        memories += [{"text":p["text"],"kind":p["kind"],"product_id":p["product_id"],"sources":p["sources"][:8],"source_count":len(p["sources"]),"created_at":p["created_at"],"supersedes":p["supersedes"],"pinned":bool(p["pinned"])} for p in ranked]
        memories += [{"text":r["text"],"kind":"original","sources":[r["record_id"]],"role":r["role"],"time":r["occurred_at"]} for r in recall if r["record_id"] not in seen]
        memories.sort(key=lambda m:(m.get("pinned",False),m["kind"]=="correction",self._score(m["text"],event.text),m["kind"]=="fact",m.get("created_at",m.get("time",0))),reverse=True)
        # Reserve half for recent dialogue; keep whole entries and expose omissions.
        def pack(items, budget):
            result, used = [], 0
            for item in items:
                cost = len(json.dumps(item,ensure_ascii=False))
                if cost+used<=budget:
                    result.append(item)
                    used += cost
            return result,used
        recent = [{"speaker":r["role"],"text":r["text"],"time":r["occurred_at"],"source":r["record_id"]} for r in history]
        recent,used = pack(recent,self.settings.context_characters//2)
        selected,cost = pack(memories,self.settings.context_characters-used)
        metrics = {"selected_sources":[r["source"] for r in recent],"selected_products":[r.get("product_id",r.get("memory_id","")) for r in selected],
                   "omitted_entries":len(history)+len(memories)-len(recent)-len(selected),"characters":used+cost,
                   "budget_characters":self.settings.context_characters,"budget_unit":"characters_not_tokens","created_at":now}
        self.store.set_setting("memory_context:"+stable_id(person,scope),metrics)
        return {"history":list(reversed(recent)),"memories":selected,"memory_epoch":epoch}

    @staticmethod
    def _score(text,query):
        terms=[]
        for word in re.findall(r"[\u4e00-\u9fff]+|[A-Za-z0-9_]{2,}",query[:300]):
            if re.fullmatch(r"[\u4e00-\u9fff]+",word):
                terms.extend(word[i:i+2] for i in range(len(word)-1))
            else:
                terms.append(word)
        return sum(word.lower() in text.lower() for word in set(terms))

    def maintain(self, now=None):
        live_clock = now is None
        now = time.time() if now is None else now
        if not self.settings.enabled or self.store.get_setting("paused",False):
            return False
        if now<self.store.get_setting("memory_retry_at",0):
            return False
        if not self.store.acquire_lease("memory_maintenance",now,900):
            return False
        job = ""
        try:
            self.store.memory_recover_jobs(now)
            self.store.memory_backfill()
            local = datetime.fromtimestamp(now,ZoneInfo(self.timezone))
            # Journal only completed calendar days. After today's review time,
            # include today's elapsed records; remaining new messages stay eligible.
            clock = daytime.fromisoformat(self.settings.review_time)
            cutoff = local.replace(hour=clock.hour,minute=clock.minute,second=0,microsecond=0)
            if local<cutoff:
                cutoff = (local-timedelta(days=1)).replace(hour=23,minute=59,second=59,microsecond=0)
            batch = self.store.memory_batch("diary",cutoff=cutoff.timestamp(),batch_size=self.settings.batch_messages,min_messages=1)
            kind = "diary"
            if not batch:
                kind = "summary"
                batch = self.store.memory_batch(kind,cutoff=now,batch_size=self.settings.batch_messages,
                    min_messages=self.settings.summary_messages,recent=self.settings.recent_messages,
                    min_characters=int(self.settings.context_characters*self.settings.pressure_ratio))
                if not batch:
                    # A few long messages can fill the budget before the normal
                    # recent-message window is full. Keep the newest two verbatim.
                    batch = self.store.memory_batch(kind,cutoff=now,batch_size=self.settings.batch_messages,
                        min_messages=2147483647,recent=min(2,self.settings.recent_messages),
                        min_characters=int(self.settings.context_characters*self.settings.pressure_ratio))
            if not batch:
                return self._observation_diary(now,cutoff.timestamp(),live_clock=live_clock) if self.observations else False
            epoch = self.store.get_setting("memory_epoch",0)
            # Bound model input by complete messages, marking oversized originals.
            packed,used = [],0
            for row in batch:
                item = {k:row[k] for k in ("record_id","role","text","occurred_at")}
                if len(item["text"])>self.settings.input_characters//2:
                    item["text"] = item["text"][:self.settings.input_characters//2]+"[原文过长，截断；不得推测省略部分]"
                cost = len(json.dumps(item,ensure_ascii=False))
                if used+cost>self.settings.input_characters:
                    break
                packed.append(item)
                used+=cost
            if not packed:
                return False
            batch = batch[:len(packed)]
            day = local.date().isoformat()
            if not self.store.memory_reserve(day,self.settings.daily_call_limit):
                self.store.set_setting("memory_health",{"state":"budget_exhausted","at":now})
                return False
            job = stable_id(kind,epoch,[r["record_id"] for r in batch])
            self.store.memory_job(job,kind,"running",now)
            first = batch[0]
            existing = self.store.memory_products(first["person_id"],first["scope"],first["purpose"],limit=100)
            corrections = [p for p in existing if p["kind"]=="correction"]
            corrections += self.store.memory_corrections([r["record_id"] for r in batch])
            corrections = list({p["product_id"]:p for p in corrections}.values())
            facts_before = {p["product_id"]:p for p in existing if p["kind"]=="fact"}
            bounded_corrections, bounded_facts = [],[]
            extra_budget = self.settings.input_characters//4
            for p in corrections:
                item = {"text":p["text"],"sources":[s for s in p["sources"] if s in {r["record_id"] for r in batch}]}
                cost = len(json.dumps(item,ensure_ascii=False))
                if cost>extra_budget:
                    # An unrepresentable correction must not be silently ignored.
                    raise PolicyError("当前批次更正信息超过输入预算，请调整批次或预算")
                bounded_corrections.append(item)
                extra_budget-=cost
            extra_budget=self.settings.input_characters//4
            for pid,p in facts_before.items():
                item={"product_id":pid,"text":p["text"],"created_at":p["created_at"]}
                cost=len(json.dumps(item,ensure_ascii=False))
                if cost<=extra_budget:
                    bounded_facts.append(item)
                    extra_budget-=cost
            result = self.model.decide("memory_consolidate",{"mode":kind,"records":packed,"corrections":bounded_corrections,
                "current_facts":bounded_facts,"language":"中文"})
            summary = self._text(result.get("summary"),4000)
            ids = {r["record_id"] for r in batch}
            products = [{"kind":kind,"text":summary,"sources":sorted(ids)}]
            facts = result.get("facts",[])
            if not isinstance(facts,list) or len(facts)>12:
                raise PolicyError("长期事实格式无效")
            user_records = {r["record_id"]:r for r in batch if r["role"]=="user"}
            for fact in facts:
                if not isinstance(fact,dict):
                    raise PolicyError("长期事实格式无效")
                text = self._text(fact.get("text"),500)
                sources = fact.get("sources")
                quote = fact.get("quote")
                if (not isinstance(sources,list) or not sources or any(not isinstance(s,str) or s not in user_records for s in sources)
                        or not isinstance(quote,str) or not quote.strip() or not any(quote in user_records[s]["text"] for s in sources)):
                    raise PolicyError("长期事实缺少有效原文依据")
                if kind=="summary":
                    supersedes = fact.get("supersedes",[])
                    if not isinstance(supersedes,list) or any(not isinstance(pid,str) or pid not in facts_before for pid in supersedes):
                        raise PolicyError("事实修订引用无效")
                    # Late historical batches must never override newer facts.
                    for pid in supersedes:
                        old_sources = self.store.memory_source_records(facts_before[pid]["sources"])
                        if old_sources and max(user_records[s]["occurred_at"] for s in sources)<max(r["occurred_at"] for r in old_sources):
                            raise PolicyError("较早记录不能修订更新的事实")
                    products.append({"kind":"fact","text":text,"sources":sources,"supersedes":supersedes})
            if kind=="diary":
                reflection = result.get("reflection","")
                if reflection:
                    products.append({"kind":"reflection","text":self._text(reflection,1500),"sources":sorted(ids)})
            committed = self.store.memory_commit(batch,products,kind,epoch,now)
            self.store.memory_job(job,kind,"done" if committed else "stale",now)
            self.store.set_setting("memory_health",{"state":"ready" if committed else "stale","at":now})
            return committed
        except (ModelError,PolicyError,ValueError,KeyError,TypeError):
            if job:
                self.store.memory_job(job,kind,"failed",now,"总结失败；原文保留，可重试")
            self.store.set_setting("memory_retry_at",now+self.settings.retry_minutes*60)
            self.store.set_setting("memory_health",{"state":"failed","at":now,"detail":"总结未完成，原文保留；稍后重试"})
            return False
        finally:
            self.store.release_lease("memory_maintenance")

    @staticmethod
    def _text(value,maximum):
        if not isinstance(value,str) or not value.strip() or len(value)>maximum or re.search(r"[\u3040-\u30ff]",value) or not re.search(r"[\u4e00-\u9fff]",value):
            raise PolicyError("记忆总结需为长度合适的中文")
        return value.strip()

    def _observation_diary(self, now, cutoff, *, live_clock=False):
        epoch = self.store.get_setting("memory_epoch",0)
        context = self.observations.diary_context(now,cutoff,self.settings.batch_messages)
        if not context:
            return False
        # Calendar days and complete records remain distinct; no raw observation
        # is archived as dialogue, a fact, a preference or an executable task.
        local = datetime.fromtimestamp(now,ZoneInfo(self.timezone))
        first_day = datetime.fromtimestamp(context["records"][0]["viewed_at"],ZoneInfo(self.timezone)).date()
        packed,used = [],0
        for row in context["records"]:
            if datetime.fromtimestamp(row["viewed_at"],ZoneInfo(self.timezone)).date()!=first_day:
                break
            cost=len(json.dumps(row,ensure_ascii=False))
            if used+cost>self.settings.input_characters:
                break
            packed.append(row);used+=cost
        if not packed:
            return False
        context={**context,"records":packed}
        if not self.store.memory_reserve(local.date().isoformat(),self.settings.daily_call_limit):
            self.store.set_setting("memory_health",{"state":"budget_exhausted","at":now})
            return False
        job=stable_id("observation_diary",epoch,[r["record_id"] for r in packed])
        self.store.memory_job(job,"observation_diary","running",now)
        try:
            result=self.model.decide("observation_diary",{"persona":self.persona,"records":packed,
                "observed_day":first_day.isoformat(),"language":"中文","max_characters":2000})
            text=self._text(result.get("diary"),2000)
            moment=time.time() if live_clock else now
            valid=(self.observation_guard(context,moment) if self.observation_guard else self.observations.diary_valid(context,moment))
            committed=bool(valid and self.store.observation_diary_commit(context,text,epoch,moment,self.observations.settings.retention_days))
            self.store.memory_job(job,"observation_diary","done" if committed else "stale",now)
            self.store.set_setting("memory_health",{"state":"ready" if committed else "stale","at":now})
            return committed
        except (ModelError,PolicyError,ValueError,KeyError,TypeError):
            self.store.memory_job(job,"observation_diary","failed",now,"观察日记未完成，保留来源等待重试")
            raise
