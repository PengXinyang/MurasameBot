"""Bounded initiative owned by the existing decision coordinator."""
from dataclasses import asdict, dataclass
from datetime import datetime
import math
from zoneinfo import ZoneInfo

from murasamebot.application.scheduling import day_start, next_window
from murasamebot.domain.models import Action, Event, ModelError, PlatformError, PolicyError, stable_id
from murasamebot.domain.topics import blocked, combined_topic, local_topic


KINDS = {"dynamic": "autonomous_post", "private": "proactive_private"}


@dataclass(frozen=True)
class AutonomySettings:
    dynamic_enabled: bool = False
    private_enabled: bool = False
    decision_interval_minutes: int = 30
    daily_decision_limit: int = 8
    max_delay_minutes: int = 120
    dynamic_daily_limit: int = 1
    private_daily_limit: int = 2
    dynamic_gap_minutes: int = 360
    private_gap_minutes: int = 240
    private_idle_minutes: int = 60
    max_post_characters: int = 300
    windows: tuple = (("09:00", "22:00"),)
    weekdays: tuple = (0, 1, 2, 3, 4, 5, 6)
    topics: tuple = ("游戏与创作", "生活中的小想法")

    @classmethod
    def from_config(cls, data):
        data = dict(data or {})
        if "windows" in data:
            data["windows"] = tuple((w["start"], w["end"]) for w in data["windows"])
        for key in ("weekdays", "topics"):
            if key in data:
                data[key] = tuple(data[key])
        return cls(**data)

    def version(self):
        return stable_id(asdict(self))

    def enabled(self, channel):
        return getattr(self, channel + "_enabled")

    def open_at(self, now, timezone):
        return next_window(now, timezone, self.windows, self.weekdays)


@dataclass(frozen=True)
class AutonomyTarget:
    platform: str
    account_id: str
    settings: AutonomySettings
    private_enabled: bool = True
    activity: object = None
    scope: str = ""

    def key(self, prefix, suffix):
        return prefix + (self.scope + ":" if self.scope else "") + suffix


class AutonomyService:
    def __init__(self, engine):
        self.engine = engine
        self.targets = None  # None retains the original single-platform API.

    def routes(self):
        e = self.engine
        return self.targets if self.targets is not None else (AutonomyTarget(
            e.settings.platform,e.settings.account_id,e.settings.autonomy,e.settings.private_messages_enabled),)

    def activity(self, target, conversation, peer, now):
        state = self.engine.store.private_activity(conversation)
        if target.activity:
            raw = target.activity(target.account_id, peer, now)
            state = {"last_at":max(state["last_at"],raw["last_at"]),"pending":state["pending"]+raw["pending"]}
        return state

    def allowed_kinds(self, now):
        e = self.engine
        return tuple(dict.fromkeys(KINDS[c] for t in self.routes() for c in KINDS
                     if t.settings.enabled(c) and (c != "private" or t.private_enabled)
                     and t.settings.open_at(now, e.settings.timezone) == now))

    def guard(self, action, now):
        if action.kind not in KINDS.values():
            return ""
        e = self.engine
        target = next((t for t in self.routes() if t.platform==action.platform and t.account_id==action.account_id),None)
        if target is None:
            return "目标平台未启用自主消息"
        s = target.settings
        channel = "private" if action.kind == "proactive_private" else "dynamic"
        if not s.enabled(channel) or action.autonomy_version != s.version():
            return "自主发布设置已关闭或变化"
        if action.not_before > now or s.open_at(now, e.settings.timezone) != now:
            return "尚未到自主发布时段"
        snapshot = e.store.autonomy_snapshot(action.kind, now, day_start(now, e.settings.timezone), action.action_id,platform=target.platform)
        if snapshot["sent_today"] >= getattr(s, channel + "_daily_limit"):
            return "自主发布当日额度已用完"
        if snapshot["last_sent"] and now - snapshot["last_sent"] < getattr(s, channel + "_gap_minutes") * 60:
            return "尚未到自主发布冷却时间"
        if channel == "private":
            if e.owner_activity:
                reason = e.owner_activity.guard(action, now)
                if reason:
                    return reason
            if not target.private_enabled:
                return "私信功能已关闭"
            if action.subject_person_id != e.settings.owner_person_id:
                return "主动私信只允许发送给已绑定主人"
            activity = self.activity(target,action.conversation_id,action.recipient_id,now)
            if activity["pending"] or (activity["last_at"] and (
                    activity["last_at"] > action.created_at or now - activity["last_at"] < s.private_idle_minutes * 60)):
                return "主人已有新消息或正在聊天，取消过时的主动开场"
        return ""

    def run(self, now, *, platforms=None):
        e = self.engine
        targets = [t for t in self.routes() if (platforms is None or t.platform in platforms)
                   and not e.paused(t.platform) and (t.settings.dynamic_enabled or t.settings.private_enabled)]
        if not targets:
            return False
        if not e.store.acquire_lease("autonomy_decision", now, 1200):
            return False
        try:
            changed = False
            for target in targets:
                changed = self._run(target,now) or changed
            return changed
        finally:
            e.store.release_lease("autonomy_decision")

    def _run(self, target, now):
        e, s = self.engine, target.settings
        changed = False
        if not any((s.dynamic_enabled,s.private_enabled)):
            return False
        for channel, kind in KINDS.items():
            if not s.enabled(channel):
                continue
            key = target.key("autonomy_status:",channel)
            state = e.store.get_setting(key, {})
            social = e.social.context(channel="initiative_private" if channel=="private" else "dynamic",now=now,due_only=True)
            social_clock = stable_id(social)
            social_due = bool(social["states"] and social_clock != state.get("social_clock"))
            if state.get("version") == s.version() and now < state.get("next_at", 0) and not social_due:
                continue
            due = s.open_at(now, e.settings.timezone)
            status = {"version": s.version(), "at": now, "next_at": max(due, now + s.decision_interval_minutes * 60), "state": "waiting"}
            status["social_clock"] = social_clock
            e.store.set_setting(key, status)
            if due != now:
                continue
            snap = e.store.autonomy_snapshot(kind, now, day_start(now, e.settings.timezone),platform=target.platform)
            if snap["pending"] or snap["created_today"] >= getattr(s, channel + "_daily_limit"):
                continue
            if snap["last_created"] and now - snap["last_created"] < getattr(s, channel + "_gap_minutes") * 60:
                continue
            binding, conversation = None, ""
            context = {"history": [], "memories": [], "memory_epoch": e.store.get_setting("memory_epoch", 0)}
            try:
                if channel == "private":
                    if not target.private_enabled:
                        status["state"] = "private_disabled"
                        continue
                    binding = e.store.binding(e.settings.owner_person_id, target.platform)
                    access = e.private_access.get(target.platform)
                    if not binding or access is None or not access.allows_private_reply(target.account_id, binding["external_id"]):
                        status["state"] = "relationship_blocked"
                        continue
                    conversation = access.private_conversation(target.account_id, binding["external_id"])
                    activity = self.activity(target,conversation,binding["external_id"],now)
                    if activity["pending"] or activity["last_at"] and now - activity["last_at"] < s.private_idle_minutes * 60:
                        status["state"] = "conversation_busy"
                        continue
                    # Context probe only: never archive a fabricated user message.
                    probe = Event(stable_id("context", target.platform, now), target.platform, target.account_id,
                                  binding["external_id"], conversation, "context_probe", "", now, "private")
                    context = e.memory_service.context(probe, e.settings.owner_person_id, True, now)
                    if e.owner_activity:
                        context.update(e.owner_activity.private_context(e.settings.owner_person_id, now))
                today = datetime.fromtimestamp(now, ZoneInfo(e.settings.timezone)).date().isoformat()
                budget_key = target.key("autonomy_decisions:",today)
                used = e.store.get_setting(budget_key, 0)
                if used >= s.daily_decision_limit:
                    status["state"] = "budget_exhausted"
                    continue
                e.store.set_setting(budget_key, used + 1)
                result, options = e.message_decision("autonomous_private" if channel == "private" else "autonomous_post", {
                    "persona": e.persona.prompt(channel == "private", private=channel == "private"),
                    **context, "topics": list(s.topics), "social_context":social,
                    "recent_public_posts": e.store.autonomy_recent_public() if channel == "dynamic" else [],
                    "current_time": datetime.fromtimestamp(now, ZoneInfo(e.settings.timezone)).isoformat(),
                    "max_delay_minutes": s.max_delay_minutes,
                    "max_characters": s.max_post_characters if channel == "dynamic" else e.settings.max_private_characters,
                }, target.platform, target.account_id, "private" if channel == "private" else "dynamic", now)
                if channel == "private":
                    e.social.capture(result,person=e.settings.owner_person_id,context=context,social_context=social,now=now)
                if result.get("decision") == "skip":
                    status["state"] = "skipped"
                    continue
                if result.get("decision") != "send":
                    raise ModelError("自主决策类型无效")
                delay = result.get("delay_minutes")
                if type(delay) is not int or not 0 <= delay <= s.max_delay_minutes:
                    raise ModelError("自主发送延迟超出允许范围")
                send_at = s.open_at(now + delay * 60, e.settings.timezone)
                if not math.isfinite(send_at) or send_at > now + s.max_delay_minutes * 60:
                    status["state"] = "outside_window"
                    continue
                text, emojis = e.message_text(result, options, s.max_post_characters if channel == "dynamic" else e.settings.max_private_characters,
                    multilingual=channel=="private" and e.private_policies.get(target.platform,{}).get("multilingual",False))
                if channel == "dynamic" and social["states"] and blocked(combined_topic(local_topic(text),result.get("topic_class"))):
                    status["state"] = "topic_blocked"
                    continue
                followups = e.social.followups(result,social)
                if social["states"] and not followups:
                    raise ModelError("社交主动消息未标明实际采用的状态ID")
                if e.store.autonomy_text_exists(kind, text,platform=target.platform):
                    status["state"] = "duplicate_skipped"
                    continue
                action = Action(stable_id(e.settings.agent_id, kind, target.platform, target.account_id, int(now), s.version()),
                    e.settings.agent_id, target.platform, target.account_id, None, text,
                    now, send_at + e.settings.action_ttl_minutes * 60, kind=kind, not_before=send_at,
                    autonomy_version=s.version(), proactive=True, emojis=emojis, evidence_level="conversation" if channel == "private" else "text",
                    visibility="private" if channel == "private" else "public", conversation_id=conversation,
                    recipient_id=binding["external_id"] if binding else "",
                    subject_person_id=e.settings.owner_person_id if binding else "", binding_version=binding["version"] if binding else 0,
                    decision_reason="自主择时私信" if channel == "private" else "自主文字动态", memory_epoch=result["_memory_epoch"],
                    owner_activity_version=context.get("owner_activity_version", ""),
                    owner_activity_sources=context.get("owner_activity_sources", ()),
                    social_refs=e.social.references(social),social_followups=followups,
                    social_version=e.settings.social.version(),social_gap_seconds=e.settings.social.followup_gap_minutes*60,
                    topic_class=result.get("topic_class","unknown"))
                if e.owner_activity and e.owner_activity.guard(action,now):
                    status["state"] = "invalidated"
                    continue
                inserted = e.enqueue_generated(action, result, now)
                changed = inserted or changed
                status.update(state="planned" if inserted else "invalidated")
                if inserted:
                    status.update(planned_at=send_at, action_id=action.action_id)
            except (ModelError, PlatformError, PolicyError, ValueError, TypeError, KeyError) as exc:
                if isinstance(exc, PlatformError):
                    e.pause_platform(exc, target.platform)
                status.update(state="error", next_at=now + e.settings.event_retry_seconds)
                e.store.audit("autonomy_error", target.platform + ": " + channel + ": " + type(exc).__name__, now)
            finally:
                e.store.set_setting(key, status)
        return changed
