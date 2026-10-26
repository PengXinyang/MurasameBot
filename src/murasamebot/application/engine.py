from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
import re
import time
from zoneinfo import ZoneInfo

from murasamebot.application.scheduling import day_start, next_window
from murasamebot.application.settings import Behaviour
from murasamebot.domain.models import Action, ContentRef, Document, Event, ModelError, Persona, PlatformError, PolicyError, Receipt, stable_id
from murasamebot.ports import ActionSink, ContentProvider, ModelGateway, StateStore


GROUNDED = {"transcript", "text", "visual", "transcript_visual"}


def checked_text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        raise PolicyError("回复正文必须为字符串")
    value = value.strip()
    if not value or len(value) > limit:
        raise PolicyError("回复为空或超过配置长度")
    if re.search(r"[\u3040-\u30ff]|【[^】]+】|<[^>]+>", value):
        raise PolicyError("当前渠道只接受中文正文，不接受日语、情绪标签或翻译标签")
    if re.search(r"https?://|www\.|@[\w\u4e00-\u9fff]", value, re.I):
        raise PolicyError("模型不得自行插入链接或指定提及对象")
    if not re.search(r"[\u4e00-\u9fff]", value):
        raise PolicyError("回复需要包含中文正文")
    return value


class SocialEngine:
    """One decision owner. Platform inputs and outputs are injected ports."""

    def __init__(self, settings: Behaviour, store: StateStore, model: ModelGateway, persona: Persona,
                 providers: Mapping[str, ContentProvider], sinks: Mapping[str, ActionSink],
                 action_policy: Callable[[Action, float], str] | None = None):
        self.settings, self.store, self.model, self.persona = settings, store, model, persona
        self.providers, self.sinks = dict(providers), dict(sinks)
        self.action_policy = action_policy or (lambda action, now: "")

    def paused(self, platform: str = "") -> bool:
        return bool(self.store.get_setting("paused", False) or (platform and self.store.get_setting(f"platform_paused:{platform}", False)))

    def pause_platform(self, exc: PlatformError, fallback: str):
        if exc.pause:
            self.store.set_setting(f"platform_paused:{exc.platform or fallback}", True)

    def ingest(self, event: Event, *, baseline: bool = False) -> bool:
        if not event.event_id or not event.actor_id or not event.conversation_id:
            raise ValueError("事件缺少稳定身份或会话标识")
        return self.store.enqueue_event(event, baseline=baseline)

    def document(self, ref: ContentRef, now: float, *, reply_to: str = "", root: str = "") -> Document:
        # Reply chains must be refreshed; cached video facts alone are insufficient.
        cached = self.store.cached_document(ref, now) if not reply_to else None
        if cached:
            return cached
        document = self.providers[ref.provider].read(ref, reply_to=reply_to, root=root)
        self.store.cache_document(document, now)
        return document

    def process_one_event(self, now: float | None = None) -> bool:
        current = time.time() if now is None else now
        if not self.store.acquire_lease("event_decision", current, 900):
            return False
        try:
            return self._process_one_event(now)
        finally:
            self.store.release_lease("event_decision")

    def _process_one_event(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        if self.paused():
            return False
        event = self.store.claim_event(now)
        if event is None:
            return False
        try:
            person = self.store.resolve_person(event.platform, event.actor_id)
            if event.kind == "chat" and event.visibility == "private" and person == self.settings.owner_person_id:
                self._owner_intent(event, person, now)
            elif event.kind == "owner_repost":
                self._owner_repost(event, now)
            elif event.kind in ("mention", "reply", "chat", "owner_content"):
                self._reply(event, person, now)
            self.store.finish_event(event.event_id, "done")
        except (ModelError, PlatformError, PolicyError, ValueError, KeyError) as exc:
            if isinstance(exc, PlatformError) and exc.pause:
                self.pause_platform(exc, event.platform)
            self.store.retry_event(event.event_id, now, str(exc), self.settings.max_event_attempts)
            self.store.audit("event_error", f"{event.event_id}: {type(exc).__name__}: {exc}", now)
        return True

    def _owner_intent(self, event: Event, person: str, now: float) -> None:
        source_binding = self.store.binding(person, event.platform)
        if not source_binding or source_binding["external_id"] != event.actor_id or source_binding["purpose"] != "owner_video_recommendation":
            self.store.audit("intent_scope_denied", "当前来源绑定未允许跨平台视频委托", now)
            return
        clock_key = f"intent_clock:{person}"
        if event.occurred_at < self.store.get_setting(clock_key, 0):
            self.store.audit("stale_intent", "较早发生的迟到需求未覆盖更新的指令", now)
            return
        tasks = self.store.active_tasks(person)
        preferences = self.store.memory(person, "person", "owner_video_recommendation", now)
        result = self.model.decide("extract_intent", {
            "message": event.text,
            "now": datetime.fromtimestamp(now, ZoneInfo(self.settings.timezone)).isoformat(),
            "timezone": self.settings.timezone,
            "active_tasks": [{"task_id": task["task_id"], "topic": task["topic"]} for task in tasks],
            "current_preferences": [row for row in preferences if row["kind"] == "preference"],
        })
        kind = result.get("kind", "none")
        current_binding = self.store.binding(person, event.platform)
        if self.store.resolve_person(event.platform, event.actor_id) != person or not current_binding or current_binding["version"] != source_binding["version"]:
            raise PolicyError("消息来源的身份绑定已经变化")
        if kind not in {"request", "update", "cancel", "preference", "none"}:
            raise PolicyError("意图类型无效")
        if kind == "none":
            return
        # Unknown model-supplied task IDs can never cancel another person's task.
        ids = result.get("task_ids", [])
        if not isinstance(ids, list):
            raise PolicyError("task_ids 必须是列表")
        known = {task["task_id"] for task in tasks}
        if any(item not in known for item in ids):
            raise PolicyError("模型引用了不属于当前主人的任务")
        forget_ids = result.get("forget_memory_ids", [])
        memory_ids = {row["memory_id"] for row in preferences if row["kind"] == "preference"}
        if not isinstance(forget_ids, list) or any(item not in memory_ids for item in forget_ids):
            raise PolicyError("模型引用了无权修改的记忆")
        if kind == "cancel":
            for task_id in ids or ([tasks[0]["task_id"]] if tasks else []):
                self.store.cancel_task(task_id)
                task = self.store.task(task_id)
                if task:
                    self.store.forget(task["memory_id"])
            for memory_id in forget_ids:
                self.store.forget(memory_id)
            self.store.set_setting(clock_key, event.occurred_at)
            return
        topic = result.get("topic")
        if not isinstance(topic, str) or not topic.strip() or len(topic) > 120:
            raise PolicyError("需要简短明确的视频主题")
        topic = topic.strip()
        sensitive = result.get("sensitive", False) is not False
        purpose = "owner_private" if sensitive else "owner_video_recommendation"
        if kind == "preference":
            for memory_id in forget_ids:
                self.store.forget(memory_id)
            self.store.add_memory(person, "person", purpose, topic, event.event_id, now, kind="preference")
            self.store.set_setting(clock_key, event.occurred_at)
            return
        binding = self.store.binding(person, self.settings.platform)
        if not binding or binding["purpose"] != "owner_video_recommendation":
            raise PolicyError("主人尚未绑定允许投递的目标账号")
        requested = result.get("not_before")
        due = now
        if requested:
            try:
                dt = datetime.fromisoformat(str(requested))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=ZoneInfo(self.settings.timezone))
                due = max(now, dt.timestamp())
            except ValueError as exc:
                raise PolicyError("需求时间无法解析") from exc
        expiry = now + self.settings.request_expiry_days * 86400
        if result.get("not_after"):
            try:
                deadline = datetime.fromisoformat(str(result["not_after"]))
                if deadline.tzinfo is None:
                    deadline = deadline.replace(tzinfo=ZoneInfo(self.settings.timezone))
                expiry = min(expiry, deadline.timestamp())
            except ValueError as exc:
                raise PolicyError("需求截止时间无法解析") from exc
        if due >= expiry:
            raise PolicyError("需求时间超过任务有效期，需要调整有效期")
        interval = result.get("interval_days", 0)
        if not isinstance(interval, int) or isinstance(interval, bool) or not 0 <= interval <= 365:
            raise PolicyError("周期参数无效")
        if interval and not re.search(r"每天|每周|每星期|每月|定期|持续|以后.*推荐", event.text):
            interval = 0
        # Validate the replacement completely before invalidating previous requests.
        if kind == "update":
            for task_id in ids or ([tasks[0]["task_id"]] if tasks else []):
                self.store.cancel_task(task_id)
                task = self.store.task(task_id)
                if task:
                    self.store.forget(task["memory_id"])
        for memory_id in forget_ids:
            self.store.forget(memory_id)
        memory_id = self.store.add_memory(person, "person", purpose, topic, event.event_id, now,
                                          kind="request", valid_until=expiry)
        self.store.add_task(person, topic, event.event_id, memory_id, now, due, expiry, binding["version"],
                            target_platform=self.settings.platform, interval_days=interval, sensitive=sensitive)
        self.store.set_setting(clock_key, event.occurred_at)

    def _reply(self, event: Event, person: str | None, now: float) -> None:
        owner_content = event.kind == "owner_content"
        if owner_content:
            binding = self.store.binding(self.settings.owner_person_id, event.platform)
            if not binding or event.actor_id != binding["external_id"]:
                raise PolicyError("主人内容事件的作者与绑定不一致")
            if event.content and self.store.has_action_for_content(event.content.key, now):
                return
        document = self.document(event.content, now, reply_to=event.reply_to, root=event.thread_root) if event.content else None
        if owner_content and document and document.author_id and document.author_id != event.actor_id:
            raise PolicyError("读取到的内容作者与主人内容事件不一致")
        if owner_content and document and document.ref.kind == "video" and document.evidence_level not in GROUNDED:
            self.store.audit("skipped", f"主人视频缺少内容证据：{document.ref.key}", now)
            return
        private_owner = person == self.settings.owner_person_id and event.visibility == "private"
        scope = f"conversation:{event.platform}:{event.conversation_id}"
        memories = self.store.memory(person or f"unlinked:{event.platform}:{event.actor_id}", scope,
                                     "owner_dialogue" if private_owner else "public_dialogue", now)
        result = self.model.decide("reply", {
            "persona": self.persona.prompt(owner_addressed=person == self.settings.owner_person_id),
            "message": event.text,
            "content": document.public_context() if document else None,
            "history": self.store.context_history(event.conversation_id),
            "memories": memories,
            "max_characters": self.settings.max_comment_characters,
            "owner_content": owner_content,
        })
        if result.get("decision") not in {"reply", "skip"}:
            raise PolicyError("回复决策类型无效")
        if result.get("decision") == "skip":
            return
        text = checked_text(result.get("text"), self.settings.max_comment_characters)
        action = Action(
            action_id=stable_id(self.settings.agent_id, "event", event.event_id), agent_id=self.settings.agent_id,
            platform=event.platform, account_id=event.account_id, content=event.content, text=text,
            created_at=now, expires_at=now + self.settings.action_ttl_minutes * 60,
            kind="owner_comment" if owner_content else "reply", reply_to=event.reply_to,
            thread_root=event.thread_root, event_id=event.event_id, conversation_id=event.conversation_id,
            evidence_level=document.evidence_level if document else "conversation",
            subject_person_id=self.settings.owner_person_id if owner_content else "",
            binding_version=binding["version"] if owner_content else 0,
            decision_reason=str(result.get("reason", ""))[:200], dedup_key=event.event_id,
        )
        self.store.enqueue_action(action, draft=self.settings.draft, now=now)

    def _owner_repost(self, event: Event, now: float) -> None:
        binding = self.store.binding(self.settings.owner_person_id, event.platform)
        if not binding or binding["external_id"] != event.actor_id or not event.content:
            raise PolicyError("主人转发事件的作者与绑定不一致")
        document = self.document(event.content, now)
        if document.author_id != event.actor_id:
            raise PolicyError("不能转发作者未验证的主人内容")
        result = self.model.decide("owner_repost", {"persona": self.persona.prompt(True),
                                   "content": document.public_context(), "max_characters": self.settings.max_comment_characters})
        if result.get("decision") == "skip":
            return
        if result.get("decision") != "share":
            raise PolicyError("转发决策类型无效")
        text = checked_text(result.get("text"), self.settings.max_comment_characters)
        action = Action(stable_id(self.settings.agent_id, "repost", event.content.key), self.settings.agent_id,
                        event.platform, event.account_id, event.content, text, now,
                        now + self.settings.action_ttl_minutes * 60, kind="owner_repost",
                        event_id=event.event_id, subject_person_id=self.settings.owner_person_id,
                        binding_version=binding["version"], evidence_level=document.evidence_level,
                        source_author_id=event.actor_id, source_kind="video" if event.share_content and event.share_content.kind == "video" else "dynamic",
                        dedup_key=f"repost:{event.content.key}", decision_reason="转发已验证主人的新内容")
        self.store.enqueue_action(action, draft=self.settings.draft, now=now)

    def run_due_task(self, now: float | None = None) -> bool:
        live_clock = now is None
        now = time.time() if now is None else now
        if self.paused(self.settings.platform) or not self.settings.browsing_enabled:
            return False
        if next_window(now, self.settings.timezone, self.settings.windows, self.settings.weekdays) > now:
            return False
        task = self.store.claim_task(now)
        if task is None:
            return False
        if task["attempts"] > self.settings.max_task_attempts:
            self.store.defer_task(task["task_id"], now, "已达到委托查找次数上限", exhausted=True)
            return True
        try:
            if task["sensitive"]:
                self.store.defer_task(task["task_id"], now, "主题不适合公开提及；请在本地管理任务", exhausted=True)
                return True
            binding = self.store.binding(task["person_id"], task["target_platform"])
            if not binding or binding["version"] != task["binding_version"]:
                self.store.cancel_task(task["task_id"])
                return True
            provider = self.providers[task["target_platform"]]
            refs = provider.search(task["topic"], self.settings.categories, self.settings.candidate_limit)
            for ref in refs[:self.settings.videos_per_round]:
                moment = time.time() if live_clock else now
                following = next_window(moment, self.settings.timezone, self.settings.windows, self.settings.weekdays)
                if following > moment:
                    self.store.defer_task(task["task_id"], following, "本次浏览时段已结束")
                    return True
                if self.paused(self.settings.platform):
                    self.store.defer_task(task["task_id"], moment + 60, "已暂停")
                    return True
                if self.store.has_action_for_content(ref.key, now):
                    continue
                document = self.document(ref, now)
                if document.evidence_level not in GROUNDED:
                    self.store.record_view(ref, "缺少可验证的正文/字幕，未生成推荐", now)
                    continue
                if self._evaluate(document, now, task=task):
                    return True
            self.store.defer_task(task["task_id"], now + self.settings.interval_minutes * 60, "未找到合适内容",
                                  exhausted=task["attempts"] >= self.settings.max_task_attempts)
        except (ModelError, PlatformError, PolicyError, KeyError, ValueError) as exc:
            if isinstance(exc, PlatformError) and exc.pause:
                self.pause_platform(exc, self.settings.platform)
            self.store.defer_task(task["task_id"], now + self.settings.interval_minutes * 60, str(exc),
                                  exhausted=task["attempts"] >= self.settings.max_task_attempts)
            self.store.audit("task_error", f"{task['task_id']}: {type(exc).__name__}: {exc}", now)
        return True

    def browse(self, now: float | None = None) -> int:
        live_clock = now is None
        now = time.time() if now is None else now
        if self.paused(self.settings.platform) or not self.settings.browsing_enabled or not self.settings.categories:
            return 0
        if next_window(now, self.settings.timezone, self.settings.windows, self.settings.weekdays) > now:
            return 0
        last = self.store.get_setting("last_browse", 0)
        if now - last < self.settings.interval_minutes * 60:
            return 0
        if not self.store.acquire_lease("browse", now, 900):
            return 0
        count = 0
        try:
            # Persist before doing slow work so restarts don't trigger a burst.
            self.store.set_setting("last_browse", now)
            refs = self.providers[self.settings.platform].discover(self.settings.categories, self.settings.candidate_limit)
            for ref in refs:
                moment = time.time() if live_clock else now
                if next_window(moment, self.settings.timezone, self.settings.windows, self.settings.weekdays) > moment:
                    break
                if count >= self.settings.videos_per_round or self.paused(self.settings.platform):
                    break
                if self.store.has_viewed(ref, now) or self.store.has_action_for_content(ref.key, now):
                    continue
                count += 1
                document = self.document(ref, now)
                if document.evidence_level not in GROUNDED:
                    self.store.record_view(ref, "无内容证据，略过主动评论", now)
                    continue
                self._evaluate(document, now)
        except (ModelError, PlatformError, PolicyError, ValueError) as exc:
            if isinstance(exc, PlatformError) and exc.pause:
                self.pause_platform(exc, self.settings.platform)
            self.store.audit("browse_error", f"{type(exc).__name__}: {exc}", now)
        finally:
            self.store.release_lease("browse")
        return count

    def _evaluate(self, document: Document, now: float, task: dict | None = None) -> bool:
        binding = self.store.binding(self.settings.owner_person_id, document.ref.provider)
        # This payload is deliberately independent of the original private message.
        result = self.model.decide("evaluate_video", {
            "persona": self.persona.prompt(), "content": document.public_context(),
            "interests": list(self.settings.interests), "owner_interests": list(self.settings.owner_interests),
            "owner_preferences": [row for row in self.store.memory(self.settings.owner_person_id, "person", "owner_video_recommendation", now) if row["kind"] == "preference"],
            "requested_topic": task["topic"] if task else None,
            "max_characters": self.settings.max_recommendation_characters if task else self.settings.max_comment_characters,
        })
        decision = result.get("decision", "skip")
        if decision not in {"skip", "comment", "reply", "recommend"}:
            raise PolicyError("视频行为选择无效")
        reason = str(result.get("reason", ""))[:200]
        self.store.record_view(document.ref, reason or decision, now)
        if decision == "skip":
            return False
        if task and decision != "recommend":
            return False
        if decision == "recommend" and (not binding or binding["external_id"] == document.author_id):
            return False
        limit = self.settings.max_recommendation_characters if decision == "recommend" else self.settings.max_comment_characters
        text = checked_text(result.get("text"), limit)
        reply_to, root = "", ""
        if decision == "reply":
            selected = next((c for c in document.comments if c.get("id") == str(result.get("reply_to", ""))), None)
            if not selected:
                raise PolicyError("只能回复实际读取的评论")
            reply_to, root = selected["id"], selected.get("root", "") or selected["id"]
        action = Action(
            action_id=stable_id(self.settings.agent_id, "task" if task else "browse", task["task_id"] if task else document.ref.key,
                                task["version"] if task else 0, document.ref.key, task["attempts"] if task else 0),
            agent_id=self.settings.agent_id, platform=document.ref.provider, account_id=self.settings.account_id,
            content=document.ref, text=text, created_at=now,
            expires_at=min(now + self.settings.action_ttl_minutes * 60, task["expires_at"] if task else float("inf")),
            kind="recommendation" if decision == "recommend" else "browse_comment", reply_to=reply_to, thread_root=root,
            mentions=(self.settings.owner_person_id,) if decision == "recommend" else (),
            task_id=task["task_id"] if task else "", task_version=task["version"] if task else 0,
            event_id=task["event_id"] if task else "", binding_version=binding["version"] if decision == "recommend" else 0,
            evidence_level=document.evidence_level, decision_reason=reason, dedup_key=f"proactive:{document.ref.key}",
        )
        return self.store.enqueue_action(action, draft=self.settings.draft, now=now)

    def dispatch_one(self, now: float | None = None) -> bool:
        live_clock = now is None
        now = time.time() if now is None else now
        if self.paused():
            return False
        action = self.store.claim_action(now, self.settings.daily_comments, self.settings.daily_recommendations,
                                         day_start(now, self.settings.timezone), self.settings.min_send_gap_seconds,
                                         self.settings.recommendation_gap_minutes * 60,
                                         self.settings.browsing_enabled and next_window(now, self.settings.timezone, self.settings.windows, self.settings.weekdays) == now,
                                         manual_only=self.settings.draft)
        if action is None:
            return False
        # Read the clock again after any preceding slow operation; test clocks remain deterministic.
        reason = self.store.validate_action(action, now)
        reason = reason or self.action_policy(action, now)
        if reason or self.paused(action.platform):
            self.store.finish_action(action.action_id, Receipt("failed", detail=reason or "paused_before_send"), now)
            return True
        mentions = {}
        for person in action.mentions:
            binding = self.store.binding(person, action.platform)
            if not binding:
                self.store.finish_action(action.action_id, Receipt("failed", detail="binding_missing"), now)
                return True
            mentions[person] = binding["external_id"]
        sink = self.sinks.get(action.platform)
        if sink is None:
            self.store.finish_action(action.action_id, Receipt("failed", detail="目标平台尚未接入"), now)
            return True
        try:
            def before_write():
                moment = time.time() if live_clock else now
                return not self.store.validate_action(action, moment) and not self.paused(action.platform) and not self.action_policy(action, moment)
            receipt = sink.send(action, mentions, before_write=before_write)
        except PlatformError as exc:
            if exc.pause:
                self.pause_platform(exc, action.platform)
            receipt = Receipt("failed", detail=str(exc))
        except Exception as exc:
            # Transport-level implementation must distinguish definite failures. Otherwise conservative.
            receipt = Receipt("unknown", detail=f"发送未能确认：{type(exc).__name__}")
        if receipt.pause:
            self.store.set_setting(f"platform_paused:{action.platform}", True)
        self.store.finish_action(action.action_id, receipt, now)
        return True
