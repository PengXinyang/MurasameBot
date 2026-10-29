"""Private observations, independent of social-message ingestion and memories."""
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from murasamebot.domain.models import PlatformError, stable_id
from murasamebot.ports import OwnerObservationSource, OwnerObservationStore
from murasamebot.application.scheduling import next_window


@dataclass(frozen=True)
class OwnerHistorySettings:
    enabled: bool = False
    poll_seconds: int = 300
    pages: int = 3
    retention_days: int = 7
    window_minutes: int = 120
    context_limit: int = 12
    use_for_private: bool = True
    playful_jealousy: bool = False
    recommendations_enabled: bool = False
    recommendation_topics: tuple = ("galgame", "千恋万花")
    recommendation_daily_limit: int = 1
    recommendation_gap_minutes: int = 360


class OwnerHistoryService:
    def __init__(self, store: OwnerObservationStore, source: OwnerObservationSource, settings: OwnerHistorySettings, person_id: str, platform: str):
        self.store, self.source, self.settings, self.person_id = store, source, settings, person_id
        self.platform = platform

    def sync(self, now, *, force=False):
        s, db = self.settings, self.store
        if not s.enabled or not db.acquire_lease("owner_history_sync", now, 180):
            return False
        state = db.owner_history_state(self.person_id)
        try:
            if not force and now - state.get("last_attempt", 0) < s.poll_seconds:
                return False
            db.owner_history_status(self.person_id, state["revision"], {"last_attempt": now})
            binding = db.binding(self.person_id, self.platform)
            if not binding:
                raise PlatformError("主人绑定已撤销", code="owner_binding_missing")
            auth = self.source.authorization(binding)
            rows, warning = self.source.read_history(binding, pages=s.pages,
                since=max(state.get("baseline_at", 0), state.get("watermark", 0) - 120))
            if self.source.authorization(binding) != auth:
                return False
            return db.owner_history_commit(self.person_id, binding, state["revision"], auth,
                                          rows, warning, now, s.retention_days)
        except (PlatformError, OSError, ValueError) as exc:
            # History failures never pause the separate AI publishing account.
            db.owner_history_status(self.person_id, state["revision"], {
                "state": "error", "error": getattr(exc, "code", "history_unavailable"), "last_attempt": now})
            return False
        finally:
            db.release_lease("owner_history_sync")

    def _observations(self, person_id, now):
        s = self.settings
        grant = self._grant_version()
        if person_id != self.person_id or not grant:
            return {}
        state = self.store.owner_history_state(person_id)
        rows = self.store.owner_history_rows(person_id, now, limit=s.context_limit,
                                             since=now-s.window_minutes*60, eligible_only=True)
        if not rows or self._grant_version() != grant:
            return {}
        return {"owner_activity": self._activity(rows, state),
                "owner_activity_version": grant,
                "owner_activity_sources": tuple(stable_id(r["content_id"],r["viewed_at"]) for r in rows),
                "owner_activity_fingerprint": stable_id(state.get("auth"),state["revision"],rows)}

    def _activity(self, rows, state):
        return {"source": "主人授权的本人观看历史", "records": rows,
                "partial": bool(state.get("warning")), "playful_jealousy": self.settings.playful_jealousy,
                "limitations": "这些是过去发生的观看线索，新增记录不否定旧观察；勿推断现在仍在观看。播放进度不是实际观看时长；不是永久偏好或委托。"}

    def _grant_version(self):
        s = self.settings
        binding = self.store.binding(self.person_id,self.platform)
        if not s.enabled or not binding:
            return ""
        state = self.store.owner_history_state(self.person_id)
        try:
            auth = self.source.authorization(binding)
        except (PlatformError, ValueError, OSError):
            return ""
        if state.get("auth") != auth or state.get("error") in {
                "owner_login_required","owner_login_invalid","owner_identity_mismatch",
                "owner_binding_missing","owner_auth_or_api_error"}:
            return ""
        # Operational polling/window limits do not revoke an established fact.
        policy = (s.use_for_private,s.playful_jealousy,s.recommendations_enabled,s.recommendation_topics)
        return "v2:"+stable_id(auth,state["revision"],self.person_id,self.platform,
                               binding["external_id"],binding["version"],policy)

    def private_context(self, person_id, now):
        return self._observations(person_id,now) if self.settings.use_for_private else {}

    def guard(self, action, now):
        if not action.owner_activity_version:
            return ""
        private = (action.kind in {"proactive_private","private_reply"} and action.visibility == "private" and self.settings.use_for_private
                   and action.subject_person_id == self.person_id)
        public = (action.kind == "recommendation" and action.visibility == "public" and
                  action.mentions == (self.person_id,) and self.settings.recommendations_enabled)
        if not (private or public) or action.platform != self.platform or self._grant_version() != action.owner_activity_version:
            return "主人观察授权或使用权限已失效；请重新生成"
        if not action.owner_activity_sources:
            return "旧消息缺少可核验的观察来源；请重新生成"
        rows = self.store.owner_history_sources(self.person_id,action.owner_activity_sources,now,self.settings.retention_days)
        if len(rows) != len(set(action.owner_activity_sources)):
            return "消息引用的主人观察已被删除或超过保留期限；请重新生成"
        return ""

    def review_context(self, action, now):
        if self.guard(action,now):
            return None
        rows = self.store.owner_history_sources(self.person_id,action.owner_activity_sources,now,self.settings.retention_days)
        return self._activity(rows,self.store.owner_history_state(self.person_id))

    def recommend(self, engine, now):
        """Only configured public topics cross into a public recommendation prompt."""
        s, e, db = self.settings, engine, self.store
        if (e.settings.platform != self.platform or not s.recommendations_enabled or not s.enabled or not e.settings.browsing_enabled
                or not e.settings.recommend_owner or e.paused(e.settings.platform)
                or next_window(now,e.settings.timezone,e.settings.windows,e.settings.weekdays)!=now):
            return False
        context = self._observations(self.person_id,now)
        if not context:
            return False
        rows = context["owner_activity"]["records"]
        topic = next((topic for topic in s.recommendation_topics if any(
            topic.casefold() in (r["title"]+" "+r["category"]).casefold() for r in rows)),None)
        if not topic:
            return False
        key = "owner_history_recommendation:"+self.person_id
        state = db.get_setting(key,{})
        day = datetime.fromtimestamp(now,ZoneInfo(e.settings.timezone)).date().isoformat()
        used = state.get("attempts",0) if state.get("day")==day else 0
        if (used>=s.recommendation_daily_limit or now-state.get("at",0)<s.recommendation_gap_minutes*60
                or state.get("version")==context["owner_activity_fingerprint"]):
            return False
        # Reserve before search/model calls: skips and failures cannot cause a burst.
        state = {"day":day,"attempts":used+1,"at":now,"version":context["owner_activity_fingerprint"],"state":"searching"}
        db.set_setting(key,state)
        try:
            refs = e.providers[e.settings.platform].search(topic,e.settings.categories,e.settings.candidate_limit)
            watched = {r["content_id"] for r in rows}
            for ref in refs[:e.settings.videos_per_round]:
                if ref.external_id in watched or db.has_action_for_content(ref.key,now):
                    continue
                document = e.document(ref,now)
                if not e.can_evaluate(document):
                    continue
                observation = {"topic":topic,"version":context["owner_activity_version"],
                               "sources":context["owner_activity_sources"],
                               "memory_epoch":db.get_setting("memory_epoch",0)}
                if e._evaluate(document,now,owner_observation=observation):
                    state["state"]="planned"
                    return True
            state["state"]="skipped"
        except (PlatformError,ValueError,RuntimeError,KeyError):
            state["state"]="error"
        finally:
            db.set_setting(key,state)
        return False
