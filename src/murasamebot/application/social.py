"""Source-backed social observations and expiring role reactions, not user facts."""
from dataclasses import asdict, dataclass

from murasamebot.domain.models import Action, PolicyError, stable_id
from murasamebot.domain.topics import local_topic


FEELINGS = {"curious", "happy", "miss_owner", "jealous", "concerned", "relieved"}
ATTENTION = {"notice", "ask", "later"}


@dataclass(frozen=True)
class SocialSettings:
    enabled: bool = True
    public_posts_enabled: bool = True
    private_feelings_public: bool = True  # owner's explicit consent; feelings only
    ttl_hours: int = 24
    retention_days: int = 7
    context_limit: int = 6
    max_active_states: int = 20
    max_followups: int = 2
    followup_min_minutes: int = 15
    followup_max_minutes: int = 720
    followup_gap_minutes: int = 120

    def version(self):
        return stable_id(asdict(self))


class SocialService:
    def __init__(self, store, settings=None, *, owner_person_id="owner", observations=None):
        self.store, self.settings = store, settings or SocialSettings()
        self.owner_person_id, self.observations = owner_person_id, observations

    def _history_guard(self, source, now):
        if not source.get("owner_activity_version"):
            return ""
        if self.observations is None:
            return "观察来源不可用"
        probe = Action("social-probe", self.store.agent_id, source["activity_platform"], "", None, "", now, now + 1,
                       kind="private_reply", visibility="private", subject_person_id=self.owner_person_id,
                       owner_activity_version=source["owner_activity_version"],
                       owner_activity_sources=tuple(source["owner_activity_sources"]))
        return self.observations.guard(probe, now)

    def context(self, *, event=None, person=None, channel="private", now, due_only=False):
        if not self.settings.enabled:
            return {"states": []}
        rows = self.store.social_rows(now, limit=100)
        states = []
        for row in rows:
            source, state = row["source"], row["state"]
            if self._history_guard(source, now):
                self.store.social_forget(row["state_id"], reason="source_invalidated")
                continue
            owner = source["subject_person_id"] == self.owner_person_id
            if due_only and (state["attention"] == "notice" or row["next_at"] > now or
                             row["followups"] >= self.settings.max_followups or self.store.social_pending(row["state_id"])):
                continue
            if channel in {"dynamic", "initiative_private"}:
                if not owner or state["followup_channel"] not in {"either", "dynamic" if channel == "dynamic" else "private"}:
                    continue
            elif event is None:
                continue
            elif channel == "private":
                same = source["person_id"] == (person or f"unlinked:{event.platform}:{event.actor_id}")
                if not (same and (source["conversation_id"] == event.conversation_id or person == self.owner_person_id)
                        or person == self.owner_person_id and owner and source["visibility"] == "public"):
                    continue
            elif not (source["visibility"] == "public" and source["platform"] == event.platform and
                       source["conversation_id"] == event.conversation_id and source["actor_id"] == event.actor_id
                       or person == self.owner_person_id and owner):
                continue
            public = channel in {"public", "dynamic"}
            if public and (channel == "dynamic" and not self.settings.public_posts_enabled or not row["public_allowed"]):
                continue
            item = {"id": row["state_id"], "version": row["version"], "feeling": state["feeling"],
                    "attention": state["attention"], "intensity": state["intensity"],
                    "followup_due": row["next_at"] <= now, "followups": row["followups"]}
            if public and source["visibility"] != "public":
                # A lossy, program-owned projection: never export notes, quotes,
                # URLs, people, history, dates, or private third-party messages.
                if not self.settings.private_feelings_public or source["person_id"] != self.owner_person_id:
                    continue
                item.update(projection="public_feeling", certainty="feeling_only",
                            guidance="只表达吾辈自己的轻微感受，可撒娇或吃醋；不能声称主人做过任何具体事情，不提线索来自私聊或观看记录")
            else:
                item.update(projection="public_event" if public else "private", certainty=source["certainty"],
                            summary=source["summary"], quote=source["quote"], source_event_id=source["event_id"],
                            source_time=source["occurred_at"],
                            source_kind=source["kind"], note=state["note"],
                            source_url=source.get("url", ""), guidance="这是带来源的观察或转述，不是已核实的用户事实；昵称和截图不能证明主人身份")
            states.append(item)
            if len(states) >= self.settings.context_limit:
                break
        return {"states": states}

    def capture(self, result, *, event=None, person=None, document=None, context=None, social_context=None, now):
        """Optional side output in the same model generation; fail independently."""
        s = self.settings
        update = result.get("social_update")
        if not s.enabled or not isinstance(update, dict):
            return {"id": "", "resolved": []}
        context, social_context = context or {}, social_context or {"states": []}
        candidates = {x["id"]: x["version"] for x in social_context["states"]}
        resolve = update.get("resolve_ids", [])
        if not isinstance(resolve, list) or any(not isinstance(x, str) or x not in candidates for x in resolve) or len(resolve) > s.context_limit:
            return self._invalid(now)
        attention = update.get("attention", "ignore")
        if not isinstance(attention,str) or attention not in ATTENTION | {"ignore", "resolve"}:
            return self._invalid(now)
        if attention == "ignore" and not resolve:
            return {"id": "", "resolved": []}
        source_kind = update.get("source", "message")
        if not isinstance(source_kind,str) or source_kind not in {"message", "visual", "content", "owner_observation"}:
            return self._invalid(now)
        owner = person == self.owner_person_id
        if event is None and (source_kind != "owner_observation" or not self.observations):
            return self._invalid(now)
        binding = self.store.binding(self.owner_person_id, event.platform if event else self.observations.platform)
        if (source_kind == "content" and not document or
                source_kind == "visual" and (not document or not document.visual or not document.visual.indices) or
                source_kind == "owner_observation" and (not owner or not self.observations or not context.get("owner_activity_version"))):
            return self._invalid(now)
        quote = update.get("quote", "")
        summary = update.get("summary", "")
        history_text = "\n".join(str(r.get("title", "")) for r in context.get("owner_activity", {}).get("records", []))
        comment_id = update.get("source_comment_id", "")
        if not isinstance(comment_id,str):
            return self._invalid(now)
        content_text = document.text if document else ""
        if source_kind == "content" and comment_id:
            comment = next((row for row in document.comments if row.get("id")==comment_id),None)
            if not comment:
                return self._invalid(now)
            content_text = comment.get("text", "")
        text = (document.visual.summary if source_kind == "visual" else content_text if source_kind == "content" else
                history_text if source_kind == "owner_observation" else event.text)
        if attention in ATTENTION and (not isinstance(quote, str) or not 2 <= len(quote) <= 300 or quote not in text or
                                       not isinstance(summary, str) or not 1 <= len(summary) <= 400):
            return self._invalid(now)
        feeling = update.get("feeling", "curious")
        intensity = update.get("intensity", 1)
        delay = update.get("followup_minutes", s.followup_min_minutes)
        channel = update.get("followup_channel", "private")
        note = update.get("note", "")
        if (not isinstance(feeling,str) or feeling not in FEELINGS or type(intensity) is not int or not 1 <= intensity <= 3 or
                type(delay) is not int or not s.followup_min_minutes <= delay <= s.followup_max_minutes or
                not isinstance(channel,str) or channel not in {"private", "dynamic", "either"} or not isinstance(note, str) or len(note) > 300):
            return self._invalid(now)
        if source_kind == "owner_observation" and feeling == "jealous":
            records = context["owner_activity"].get("records", [])
            if not context["owner_activity"].get("playful_jealousy") or len({r["content_id"] for r in records}) < 3:
                return self._invalid(now)
        actor = person or (f"unlinked:{event.platform}:{event.actor_id}" if event else self.owner_person_id)
        related = bool(binding and (owner or update.get("owner_related") is True and event.visibility == "public"))
        source = {"kind": source_kind, "event_id": event.event_id if event else "", "person_id": actor,
                  "subject_person_id": self.owner_person_id if related else actor,
                  "platform": event.platform if event else self.observations.platform,
                  "account_id": event.account_id if event else "", "actor_id": event.actor_id if event else binding["external_id"],
                  "conversation_id": event.conversation_id if event else "", "visibility": event.visibility if event else "private",
                  "binding_version": binding["version"] if owner and binding else 0,
                  "certainty": "observation" if source_kind == "owner_observation" else "owner_statement" if owner and source_kind=="message" else "reported",
                  "summary": summary, "quote": quote, "url": document.url if document else "",
                  "occurred_at": event.occurred_at if event else now}
        if document and source_kind in {"visual","content"}:
            source.update(content=asdict(document.ref),source_comment_id=comment_id)
        actor_binding = self.store.binding(person, source["platform"]) if person else None
        source["binding_version"] = actor_binding["version"] if actor_binding else 0
        if related:
            source.update(owner_binding_platform=source["platform"],owner_binding_version=binding["version"])
        if source_kind == "owner_observation":
            anchor = self.store.owner_history_state(self.owner_person_id)
            history_binding = self.store.binding(self.owner_person_id,self.observations.platform)
            source.update(owner_activity_version=context["owner_activity_version"],
                          owner_activity_sources=list(context["owner_activity_sources"]), activity_platform=self.observations.platform,
                          activity_anchor={**{k:anchor.get(k) for k in ("revision","auth")},
                                           "binding_version":history_binding["version"] if history_binding else 0})
            if self._history_guard(source, now):
                return self._invalid(now)
        public_allowed = local_topic(text, summary, note) == "casual" and (
            source["visibility"] == "public" or related and owner and s.private_feelings_public)
        source_id = stable_id("social", source["platform"], event.event_id if event else
                              (source["owner_activity_version"], source["owner_activity_sources"]))
        state = {"attention": attention, "feeling": feeling, "intensity": intensity,
                 "note": note, "followup_channel": channel}
        return self.store.social_commit(source_id, source, state, candidates, resolve, attention in ATTENTION,
                public_allowed, now, s.ttl_hours, s.retention_days, s.max_active_states, delay,
                context.get("memory_epoch", self.store.get_setting("memory_epoch", 0)), owner)

    def _invalid(self, now):
        self.store.audit("social_update_invalid", "社交侧输出无效，保留原回复流程", now)
        return {"id": "", "resolved": []}

    def references(self, context, captured=None, *, public=False):
        captured = captured or {"id": "", "resolved": []}
        refs = [{"id": x["id"], "version": x["version"], "projection": x["projection"]}
                for x in context.get("states", []) if x["id"] not in captured["resolved"]]
        if captured["id"] and (not public or captured.get("public_allowed")):
            refs.append({"id": captured["id"], "version": 1, "projection": "current"})
        return tuple(refs)

    def followups(self, result, context, captured=None, *, public=False):
        ids = result.get("social_state_ids", [])
        allowed = {x["id"] for x in context.get("states", [])}
        if not isinstance(ids, list) or any(not isinstance(x, str) or x not in allowed for x in ids):
            return ()
        ids = [sid for sid in ids if not captured or sid not in captured["resolved"]]
        if (captured and captured["id"] and (not public or captured.get("public_allowed"))
                and result.get("social_update", {}).get("attention") == "ask"):
            ids = [*ids, captured["id"]]
        return tuple(dict.fromkeys(ids))

    def guard(self, action, now):
        if not action.social_refs:
            return ""
        s = self.settings
        if not s.enabled or action.social_version != s.version():
            return "社交状态设置已关闭或变化"
        if action.kind == "autonomous_post" and not s.public_posts_enabled:
            return "公开角色互动已关闭"
        for row in self.store.social_sources(action.social_refs):
            if (action.visibility == "public" and row["visibility"] != "public" and
                    not s.private_feelings_public):
                return "私人感受公开表达已关闭"
            if self._history_guard(row, now):
                return "社交事件引用的观察授权或来源已失效"
        if action.kind in {"autonomous_post","proactive_private"}:
            rows = {row["state_id"]:row for row in self.store.social_rows(now)}
            for sid in action.social_followups:
                row = rows.get(sid)
                if not row or row["followups"] >= s.max_followups or row["next_at"] > now:
                    return "角色状态已结束、已到跟进上限或仍在冷却"
        return self.store.social_validate(action, now)

    def review_context(self, action, now):
        if self.guard(action, now):
            return None
        return self.store.social_action_context(action)
