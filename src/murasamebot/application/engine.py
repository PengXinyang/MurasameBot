from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime
import re
import time
from zoneinfo import ZoneInfo

from murasamebot.application.scheduling import day_start, next_window
from murasamebot.application.settings import Behaviour
from murasamebot.application.recommendation_policy import popularity_allows
from murasamebot.application.vision import VisionService
from murasamebot.application.expressions import ExpressionService
from murasamebot.application.memory import MemoryService
from murasamebot.application.autonomy import AutonomyService
from murasamebot.application.social import SocialService
from murasamebot.domain.topics import blocked, combined_topic, document_topic, local_topic
from murasamebot.application.review import inline_review, review_satisfied
from murasamebot.domain.models import Action, ContentRef, Document, Event, ModelError, Persona, PlatformError, PolicyError, Receipt, stable_id
from murasamebot.ports import ActionSink, ContentProvider, ModelGateway, PrivateMessageAccess, StateStore


GROUNDED = {"transcript", "text", "visual", "text_visual", "transcript_visual"}


def checked_text(value: object, limit: int, *, multilingual=False) -> str:
    if not isinstance(value, str):
        raise PolicyError("回复正文必须为字符串")
    value = value.strip()
    if not value or len(value) > limit:
        raise PolicyError("回复为空或超过配置长度")
    if not multilingual and re.search(r"[\u3040-\u30ff]|【[^】]+】|<[^>]+>", value):
        raise PolicyError("当前渠道只接受中文正文，不接受日语、情绪标签或翻译标签")
    if re.search(r"https?://|www\.|@[\w\u4e00-\u9fff]", value, re.I):
        raise PolicyError("模型不得自行插入链接或指定提及对象")
    if not multilingual and not re.search(r"[\u4e00-\u9fff]", value):
        raise PolicyError("回复需要包含中文正文")
    return value


class SocialEngine:
    """One decision owner. Platform inputs and outputs are injected ports."""

    def __init__(self, settings: Behaviour, store: StateStore, model: ModelGateway, persona: Persona,
                 providers: Mapping[str, ContentProvider], sinks: Mapping[str, ActionSink],
                 action_policy: Callable[[Action, float], str] | None = None,
                 private_access: Mapping[str, PrivateMessageAccess] | None = None,
                 vision: VisionService | None = None, expressions: ExpressionService | None = None,
                 memory: MemoryService | None = None, owner_activity=None):
        self.request_route = None
        self.event_handlers = {}
        self.content_urls = {}
        self.routing = None  # Optional composition-owned platform/account routes.
        self.owner_activity = owner_activity
        self.settings, self.store, self.model, self.persona = settings, store, model, persona
        self.providers, self.sinks = dict(providers), dict(sinks)
        self.action_policy = action_policy or (lambda action, now: "")
        self.private_access = dict(private_access or {})
        self.private_visions = {}
        self.private_policies = {}
        self.private_content_providers = {}
        self.vision = vision
        self.expressions = expressions
        self.memory_service = memory or MemoryService(store,model,timezone=settings.timezone)
        self.autonomy = AutonomyService(self)
        self.social = SocialService(store, settings.social, owner_person_id=settings.owner_person_id, observations=owner_activity)

    def message_decision(self, operation, payload, platform, account_id, channel, now):
        if channel == "private":
            payload = {**payload,"private_policy":dict(self.private_policies.get(platform,{}))}
        if operation in {"reply","private_chat","autonomous_post","autonomous_private"}:
            social = self.settings.social
            payload = {**payload,"social_rules":{"enabled":social.enabled,"ttl_hours":social.ttl_hours,
                "followup_min_minutes":social.followup_min_minutes,"followup_max_minutes":social.followup_max_minutes,
                "public_posts_enabled":social.public_posts_enabled,"private_feelings_public":social.private_feelings_public}}
        options = self.expressions.options(platform, account_id, channel, now) if self.expressions else ()
        payload = {**payload, "emoji_options": [{"key": e.key, "label": e.label, "token": e.token, "kind":e.kind} for e in options],
                   "max_emojis": self.expressions.settings.max_per_message if self.expressions else 0}
        result = self.model.decide(operation, payload)
        epoch = payload.get("memory_epoch",-1)
        if epoch>=0 and self.store.get_setting("memory_epoch",0)!=epoch:
            raise PolicyError("生成期间记忆来源发生变化，请重新生成")
        return {**result,"_memory_epoch":epoch}, options

    def message_text(self, result, options, limit, *, multilingual=False):
        text, emojis = self.expressions.compose(result, options, limit) if self.expressions else (result.get("text"), ())
        text = checked_text(text, limit, multilingual=multilingual)
        return text + "".join(e.token for e in emojis), emojis

    def enqueue_generated(self, action, result, now):
        if self.social.guard(action,now):
            return False
        if action.visibility == "public" and action.content and local_topic(action.text) == "sensitive":
            return False
        if action.owner_activity_version and (not self.owner_activity or self.owner_activity.guard(action,now)):
            return False
        draft = self.settings.draft
        if self.settings.model_review and self.settings.review_strategy=="inline":
            action,needs_human = inline_review(action,result)
            draft = draft or needs_human
        return self.store.enqueue_action(action,draft=draft,now=now)

    def paused(self, platform: str = "") -> bool:
        return bool(self.store.get_setting("paused", False) or (platform and self.store.get_setting(f"platform_paused:{platform}", False)))

    def queue_manual_send(self, action_id: str, now: float | None = None):
        now = time.time() if now is None else now
        original = self.store.manual_send_target(action_id)
        routes = self.routing() if self.routing else ((self.settings.platform, self.settings.account_id),)
        if not any(original.platform == platform and original.account_id == account for platform, account in routes):
            raise ValueError("登录账号或平台已变化，不能发送旧消息")
        if self.paused(original.platform):
            raise ValueError("工作已暂停，请先核对原因并恢复；重发不会自动解除暂停")
        action = replace(original,approved_manually=True,not_before=max(now,original.created_at),retry_at=0,
                         expires_at=now+self.settings.action_ttl_minutes*60,manual_attempts=original.manual_attempts+1)
        reason = self.action_policy(action,now) or self.autonomy.guard(action,now) or self.social.guard(action,now)
        if action.owner_activity_version:
            reason = reason or (self.owner_activity.guard(action,now) if self.owner_activity else "主人观察服务不可用")
        if reason:
            raise ValueError("消息不能发送："+reason)
        return self.store.requeue_action(original,action,now,self.settings.min_send_gap_seconds)

    def pause_platform(self, exc: PlatformError, fallback: str):
        if exc.pause:
            self.store.set_setting(f"platform_paused:{exc.platform or fallback}", True)
            self.store.set_setting(f"platform_pause_reason:{exc.platform or fallback}",{"code":exc.code,"at":time.time()})
            self.store.audit("platform_paused",f"{exc.platform or fallback}: {exc.code}",time.time())

    def ingest(self, event: Event, *, baseline: bool = False) -> bool:
        if not event.event_id or not event.actor_id or not event.conversation_id:
            raise ValueError("事件缺少稳定身份或会话标识")
        return self.store.enqueue_event(event, baseline=baseline)

    def document(self, ref: ContentRef, now: float, *, reply_to: str = "", root: str = "", context: ContentRef | None = None, private=False) -> Document:
        # Reply chains must be refreshed; cached video facts alone are insufficient.
        cached = self.store.cached_document(ref, now)
        refresh_media = cached and self.vision and self.vision.needs_refresh(cached, now)
        refresh_stats = cached and ref.kind == "video" and any(
            minimum is not None and type(value) is not int for value,minimum in
            ((cached.view_count,self.settings.min_video_views),(cached.comment_count,self.settings.min_video_comments)))
        provider = self.private_content_providers.get(ref.provider,self.providers[ref.provider]) if private else self.providers[ref.provider]
        document = cached if cached and not private and not refresh_media and not refresh_stats and not reply_to and context is None else provider.read(ref, reply_to=reply_to, root=root, context=context)
        if self.vision:
            document = self.vision.enrich(document, now, previous=cached)
        self.store.cache_document(document, now)
        return document

    def can_evaluate(self, document: Document) -> bool:
        return document.evidence_level in GROUNDED or (document.ref.kind == "video" and self.settings.allow_metadata_video and
                                                      document.evidence_level == "metadata" and bool(document.title or document.text))

    def process_one_event(self, now: float | None = None) -> bool:
        # The repository claims one event atomically and serializes each conversation.
        return self._process_one_event(now)

    def _process_one_event(self, now: float | None = None) -> bool:
        live_clock = now is None
        now = time.time() if now is None else now
        if self.paused():
            return False
        event = self.store.claim_event(now, **({"routes": self.routing()} if self.routing else {}))
        if event is None:
            return False
        try:
            person = self.store.resolve_person(event.platform, event.actor_id)
            if event.kind in self.event_handlers:
                self.event_handlers[event.kind](event,person,now)
            elif event.kind == "private_message":
                if self._private_message(event, person, now) is False:
                    self.store.finish_event(event.event_id,"denied")
                    return True
            elif event.kind == "chat" and event.visibility == "private" and person == self.settings.owner_person_id:
                self._owner_intent(event, person, now)
            elif event.kind == "owner_repost":
                self._owner_repost(event, now)
            elif event.kind in ("mention", "post_mention", "reply", "chat", "owner_content"):
                self._reply(event, person, now)
            self.store.finish_event(event.event_id, "done")
        except (ModelError, PlatformError, PolicyError, ValueError, KeyError) as exc:
            if isinstance(exc, PlatformError) and exc.pause:
                self.pause_platform(exc, event.platform)
            failed_at = time.time() if live_clock else now
            self.store.retry_event(event.event_id, failed_at, str(exc), self.settings.max_event_attempts,
                                   delay_seconds=self.settings.event_retry_seconds)
            self.store.audit("event_error", f"{event.event_id}: {type(exc).__name__}: {exc}", failed_at)
        return True

    def _owner_intent(self, event: Event, person: str, now: float) -> dict:
        source_binding = self.store.binding(person, event.platform)
        if not source_binding or source_binding["external_id"] != event.actor_id or source_binding["purpose"] != "owner_video_recommendation":
            self.store.audit("intent_scope_denied", "当前来源绑定未允许跨平台视频委托", now)
            return {"kind": "none", "status": "not_authorized"}
        clock_key = f"intent_clock:{person}"
        if event.occurred_at < self.store.get_setting(clock_key, 0):
            self.store.audit("stale_intent", "较早发生的迟到需求未覆盖更新的指令", now)
            return {"kind": "none", "status": "stale"}
        tasks = self.store.active_tasks(person)
        preferences = self.store.memory(person, "person", "owner_video_recommendation", now)
        plain = re.sub(r"第\d+条：", "", event.text).strip()
        simple_cancel = bool(self.request_route and event.platform=="qq" and tasks and re.fullmatch(
            r"(?:算了[，, ]*)?(?:不想看了|不用找了|不用找视频了|取消任务|全部取消|都不用找了|都不想看了)[。！! ]*", plain))
        result = {"kind":"cancel"} if simple_cancel else self.model.decide("extract_intent", {
            "message": event.text,
            "now": datetime.fromtimestamp(now, ZoneInfo(self.settings.timezone)).isoformat(),
            "timezone": self.settings.timezone,
            "active_tasks": [{"task_id": task["task_id"], "topic": task["topic"], "delivery": task.get("route",{}).get("mode", "mention")} for task in tasks],
            "current_preferences": [row for row in preferences if row["kind"] == "preference"],
            "private_policy":dict(self.private_policies.get(event.platform,{})) if event.visibility=="private" else {},
        })
        kind = result.get("kind", "none")
        current_binding = self.store.binding(person, event.platform)
        if self.store.resolve_person(event.platform, event.actor_id) != person or not current_binding or current_binding["version"] != source_binding["version"]:
            raise PolicyError("消息来源的身份绑定已经变化")
        if kind not in {"request", "update", "cancel", "preference", "none"}:
            raise PolicyError("意图类型无效")
        if kind == "none":
            return {"kind": "none"}
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
            if not ids and re.search(r"全部取消|都不用找|都不想看|全都取消",event.text):
                ids = list(known)
            for task_id in ids or ([tasks[0]["task_id"]] if tasks else []):
                self.store.cancel_task(task_id)
                task = self.store.task(task_id)
                if task:
                    self.store.forget(task["memory_id"])
            for memory_id in forget_ids:
                self.store.forget(memory_id)
            self.store.set_setting(clock_key, event.occurred_at)
            return {"kind": "cancel", "status": "saved"}
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
            return {"kind": "preference", "status": "saved"}
        route = self.request_route(event, person) if self.request_route else None
        if not route and self.routing and self.settings.platform not in self.providers:
            raise PolicyError("当前未启用内容查找平台，视频委托尚不能执行")
        binding = self.store.binding(person, route["platform"] if route else self.settings.platform)
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
        task_id = self.store.add_task(person, topic, event.event_id, memory_id, now, due, expiry, binding["version"],
                            target_platform=route["platform"] if route else self.settings.platform, interval_days=interval, sensitive=sensitive,
                            **({"route":route} if route else {}))
        self.store.set_setting(clock_key, event.occurred_at)
        return {"kind": kind, "status": "saved", "task_id":task_id, "due_at": due, "expires_at": expiry,
                "delivery_platform":route["platform"] if route else self.settings.platform, "content_provider":route["provider"] if route else self.settings.platform}

    def _private_message(self, event: Event, person: str | None, now: float) -> None:
        access = self.private_access.get(event.platform)
        if (event.visibility != "private" or event.content is not None or event.actor_id == event.account_id or
                access is None or not access.allows_private_reply(event.account_id, event.actor_id)):
            self.store.audit("private_denied", f"{event.event_id}: 私信关系校验未通过，未生成回复", now)
            return False
        owner = person == self.settings.owner_person_id
        binding = self.store.binding(person, event.platform) if owner else None
        intent = {"kind": "none"}
        if owner:
            checkpoint = f"private_intent:{event.event_id}"
            intent = self.store.get_setting(checkpoint)
            if intent is None:
                try:
                    intent = self._owner_intent(event, person, now)
                except PolicyError as exc:
                    # A normal chat can continue when a request needs clarification.
                    intent = {"kind": "none", "status": "not_saved", "reason": str(exc)}
                self.store.set_setting(checkpoint, intent)
        scope = f"conversation:{event.platform}:{event.conversation_id}"
        context = self.memory_service.context(event,person,owner,now)
        if owner and self.owner_activity and event.platform == self.settings.platform:
            context.update(self.owner_activity.private_context(person,now))
        visual_context = {}
        if event.images:
            service = self.private_visions.get(event.platform)
            if service:
                image_document = service.enrich(Document(ContentRef(event.platform,"private_images",event.event_id),
                    "用户本轮私聊图片",event.text,"text",images=event.images),now)
                visual_context = {"private_images": image_document.public_context()}
            else:
                visual_context = {"private_images": {"status":"unavailable", "note":"未配置图片理解，不能猜测图片内容"}}
        social = self.social.context(event=event,person=person,channel="private",now=now)
        result, options = self.message_decision("private_chat", {
            "persona": self.persona.prompt(owner, private=True), "message": event.text,
            **visual_context, "max_reply_messages": event.reply_limit,
            "reply_format": "可返回messages字符串数组，按顺序分条回复；每条独立完整，合计不超过max_characters",
            "owner_addressed": owner, **context, "social_context": social,
            "intent_result": intent, "browsing_enabled": self.settings.browsing_enabled,
            "explicit_video_requests_enabled": bool(event.platform in self.private_policies and self.request_route and self.providers),
            "max_characters": self.settings.max_private_characters,
        }, event.platform, event.account_id, "private", now)
        if result.get("decision") == "skip":
            self.social.capture(result,event=event,person=person,context=context,social_context=social,now=now)
            self.store.audit("private_skipped", f"{event.event_id}: 本条无需回复", now)
            return
        if result.get("decision") != "reply":
            raise PolicyError("私信回复决策类型无效")
        parts = result.get("messages") if event.reply_limit > 1 else None
        if parts is not None:
            if not isinstance(parts,list) or not 1<=len(parts)<=event.reply_limit or any(not isinstance(t,str) or not t.strip() for t in parts):
                raise PolicyError("分条私聊回复格式无效")
            joined = "\n".join(parts)
            raw_body = result.get("text", joined)
            if not isinstance(raw_body,str) or raw_body.strip() != joined or result.get("emojis", []):
                raise PolicyError("分条回复与完整审核正文不一致")
            result = {**result,"text":joined,"emojis":[]}
            options = ()
        text, emojis = self.message_text(result, options, self.settings.max_private_characters,
            multilingual=self.private_policies.get(event.platform,{}).get("multilingual",False))
        if owner:
            current = self.store.binding(person, event.platform)
            if not binding or not current or current["version"] != binding["version"]:
                raise PolicyError("私聊生成期间主人身份绑定已变化")
        captured = self.social.capture(result,event=event,person=person,context=context,social_context=social,now=now)
        refs = self.social.references(social,captured)
        action = Action(stable_id(self.settings.agent_id, "private", event.event_id), self.settings.agent_id,
                        event.platform, event.account_id, None, text, now, now + self.settings.action_ttl_minutes * 60,
                        kind="private_reply", reply_to=event.reply_to, event_id=event.event_id,
                        conversation_id=event.conversation_id, recipient_id=event.actor_id, visibility="private",
                        source_sequence=event.sequence, evidence_level="conversation", dedup_key=event.event_id,
                        subject_person_id=person if owner else "", binding_version=binding["version"] if binding else 0,
                        decision_reason="互关私信：结合当前消息与同会话上下文自然接话", emojis=emojis,
                        memory_epoch=result["_memory_epoch"], owner_activity_version=context.get("owner_activity_version",""),
                        owner_activity_sources=context.get("owner_activity_sources",()),
                        social_refs=refs, social_followups=self.social.followups(result,social,captured),
                        social_version=self.settings.social.version(),social_gap_seconds=self.settings.social.followup_gap_minutes*60)
        if parts:
            for i,part in enumerate(parts):
                self.enqueue_generated(replace(action,action_id=stable_id(action.action_id,i),text=part,
                    emojis=(),dedup_key=stable_id(event.event_id,i)),{**result,"text":part},now)
        else:
            self.enqueue_generated(action,result,now)

    def _reply(self, event: Event, person: str | None, now: float) -> None:
        owner = person == self.settings.owner_person_id
        if blocked(local_topic(event.text),owner):
            self.store.audit("topic_skipped","当前消息触发公开话题限制",now)
            return
        owner_content = event.kind == "owner_content"
        if owner_content and self.settings.recommendation_only:
            self.store.audit("skipped", "仅推荐模式：不主动评论主人作品", now)
            return
        if owner_content:
            binding = self.store.binding(self.settings.owner_person_id, event.platform)
            if not binding or event.actor_id != binding["external_id"]:
                raise PolicyError("主人内容事件的作者与绑定不一致")
            if event.content and self.store.has_action_for_content(event.content.key, now):
                return
        document = self.document(event.content, now, reply_to=event.reply_to, root=event.thread_root, context=event.share_content) if event.content else None
        if document and document.ref.kind == "video" and event.kind != "mention" and not popularity_allows(document,self.settings.min_video_views,self.settings.min_video_comments):
            self.store.audit("skipped", "视频统计未达门槛；仅视频评论区直接@可豁免", now)
            return
        local = document_topic(document,event.text)
        if blocked(local,owner):
            self.store.audit("topic_skipped","原内容或父评论链触发公开话题限制",now)
            return
        if owner_content and document and document.author_id and document.author_id != event.actor_id:
            raise PolicyError("读取到的内容作者与主人内容事件不一致")
        if owner_content and document and document.ref.kind == "video" and not self.can_evaluate(document):
            self.store.audit("skipped", f"主人视频缺少内容证据：{document.ref.key}", now)
            return
        private_owner = person == self.settings.owner_person_id and event.visibility == "private"
        scope = f"conversation:{event.platform}:{event.conversation_id}"
        context = self.memory_service.context(event,person,private_owner,now)
        social = self.social.context(event=event,person=person,channel="public",now=now)
        result, options = self.message_decision("reply", {
            "persona": self.persona.prompt(owner_addressed=person == self.settings.owner_person_id),
            "message": event.text,
            "reply_target": {"id": event.reply_to, "root": event.thread_root,
                             "author_id": event.actor_id, "author_name": event.actor_name},
            "content": document.public_context() if document else None,
            **context, "social_context":social,
            "max_characters": self.settings.max_comment_characters,
            "owner_content": owner_content,
            "topic_owner_exception": owner,
        }, event.platform, event.account_id, "public", now)
        if result.get("decision") not in {"reply", "skip"}:
            raise PolicyError("回复决策类型无效")
        topic = combined_topic(local,result.get("topic_class"))
        if blocked(topic,owner):
            reason = {"unknown": "模型话题分类缺失、无效或未知，按规则跳过",
                      "sensitive": "敏感公开话题，按规则跳过",
                      "serious": "非主人严肃话题，按规则跳过"}[topic]
            self.store.audit("topic_skipped",reason,now)
            return
        if document:
            document = replace(document,topic_class=topic)
        captured = self.social.capture(result,event=event,person=person,document=document,context=context,social_context=social,now=now)
        if result.get("decision") == "skip":
            if document:
                self._queue_reactions(document, now, result.get("interested") is True, event=event,memory_epoch=result["_memory_epoch"])
            return
        text, emojis = self.message_text(result, options, self.settings.max_comment_characters)
        action = Action(
            action_id=stable_id(self.settings.agent_id, "event", event.event_id), agent_id=self.settings.agent_id,
            platform=event.platform, account_id=event.account_id, content=event.content, text=text,
            created_at=now, expires_at=now + self.settings.action_ttl_minutes * 60,
            kind="owner_comment" if owner_content else "reply", reply_to=event.reply_to,
            thread_root=event.thread_root, event_id=event.event_id, conversation_id=event.conversation_id,
            evidence_level=document.evidence_level if document else "conversation",
            subject_person_id=self.settings.owner_person_id if owner_content else "",
            binding_version=binding["version"] if owner_content else 0,
            decision_reason=str(result.get("reason", ""))[:200], dedup_key=event.event_id, emojis=emojis,
            memory_epoch=result["_memory_epoch"],
            topic_class=topic,
            social_refs=self.social.references(social,captured,public=True),
            social_followups=self.social.followups(result,social,captured,public=True),
            social_version=self.settings.social.version(),social_gap_seconds=self.settings.social.followup_gap_minutes*60,
        )
        self.enqueue_generated(action,result,now)
        if document:
            # Publish the response decision before optional reactions become
            # claimable, so the sender cannot spend the minute on a coin first.
            self._queue_reactions(document, now, result.get("interested") is True, event=event,memory_epoch=result["_memory_epoch"])

    def _owner_repost(self, event: Event, now: float) -> None:
        binding = self.store.binding(self.settings.owner_person_id, event.platform)
        if not binding or binding["external_id"] != event.actor_id or not event.content:
            raise PolicyError("主人转发事件的作者与绑定不一致")
        document = self.document(event.content, now)
        if document.author_id != event.actor_id:
            raise PolicyError("不能转发作者未验证的主人内容")
        if blocked(document_topic(document),True):
            return
        result, options = self.message_decision("owner_repost", {"persona": self.persona.prompt(True),
                                   "topic_owner_exception": True,
                                   "content": document.public_context(), "max_characters": self.settings.max_comment_characters},
                                   event.platform, event.account_id, "dynamic", now)
        if result.get("decision") == "skip":
            return
        if result.get("decision") != "share":
            raise PolicyError("转发决策类型无效")
        topic = combined_topic(document_topic(document),result.get("topic_class"))
        if blocked(topic,True):
            return
        text, emojis = self.message_text(result, options, self.settings.max_comment_characters)
        action = Action(stable_id(self.settings.agent_id, "repost", event.content.key), self.settings.agent_id,
                        event.platform, event.account_id, event.content, text, now,
                        now + self.settings.action_ttl_minutes * 60, kind="owner_repost",
                        event_id=event.event_id, subject_person_id=self.settings.owner_person_id,
                        binding_version=binding["version"], evidence_level=document.evidence_level,
                        source_author_id=event.actor_id, source_kind="video" if event.share_content and event.share_content.kind == "video" else "dynamic",
                        dedup_key=f"repost:{event.content.key}", decision_reason="转发已验证主人的新内容", emojis=emojis,topic_class=topic)
        self.enqueue_generated(action,result,now)

    def _queue_reactions(self, document: Document, now: float, interested: bool, *, memory_epoch: int, event: Event | None = None) -> None:
        if document_topic(document) == "sensitive":
            return
        if not interested or not self.can_evaluate(document):
            return
        kinds = []
        if document.ref.kind == "video":
            if self.settings.video_like:
                kinds.append("like")
            if self.settings.video_coin:
                kinds.append("coin")
        elif document.ref.kind in {"dynamic", "draw", "post"} and self.settings.dynamic_like:
            kinds.append("like")
        # Parent references normalize different comment IDs to the same actual post.
        target = document.parent or document.ref
        for kind in kinds:
            action = Action(stable_id(self.settings.agent_id, self.settings.account_id, kind, target.key), self.settings.agent_id,
                            document.ref.provider, self.settings.account_id, document.ref,
                            "为感兴趣的内容点赞" if kind == "like" else "为感兴趣的视频投 1 枚硬币", now,
                            now + self.settings.action_ttl_minutes * 60, kind=kind, context=document.parent,
                            amount=1 if kind == "coin" else 0, proactive=event is None,
                            event_id=event.event_id if event else "", evidence_level=document.evidence_level,
                            decision_reason="根据实际读取的内容判断感兴趣", dedup_key=f"{kind}:{target.key}", topic_class=document.topic_class,
                            memory_epoch=memory_epoch)
            self.store.enqueue_action(action, draft=self.settings.draft, now=now)

    def run_due_task(self, now: float | None = None) -> bool:
        live_clock = now is None
        now = time.time() if now is None else now
        if self.paused(self.settings.platform) or (not self.settings.browsing_enabled and not self.request_route):
            return False
        if next_window(now, self.settings.timezone, self.settings.windows, self.settings.weekdays) > now:
            return False
        task = self.store.claim_task(now, **({"routed_only":not self.settings.browsing_enabled,
            "available_providers":tuple(self.providers),
            "available_targets":tuple(p for p,a in self.routing()) if self.routing else None} if self.request_route else {}))
        if task is None:
            return False
        if task["attempts"] > self.settings.max_task_attempts:
            self.store.defer_task(task["task_id"], now, "已达到委托查找次数上限", exhausted=True)
            return True
        try:
            private_topics = self.unfiltered_private_task(task)
            if task["sensitive"] and not private_topics:
                self.store.defer_task(task["task_id"], now, "主题不适合公开提及；请在本地管理任务", exhausted=True)
                return True
            binding = self.store.binding(task["person_id"], task["target_platform"])
            if not binding or binding["version"] != task["binding_version"]:
                self.store.cancel_task(task["task_id"])
                return True
            provider_name = task.get("route",{}).get("provider",task["target_platform"])
            provider = self.private_content_providers.get(provider_name,self.providers[provider_name]) if private_topics else self.providers[provider_name]
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
                if task.get("route",{}).get("mode")!="private" and self.store.has_action_for_content(ref.key, now):
                    continue
                document = self.document(ref, now, private=private_topics)
                if not self.can_evaluate(document):
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
                if not self.can_evaluate(document):
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

    def unfiltered_private_task(self, task):
        route = task.get("route",{}) if task else {}
        return route.get("mode")=="private" and self.private_policies.get(route.get("platform"),{}).get("topic_filter") is False

    def _evaluate(self, document: Document, now: float, task: dict | None = None, *, owner_observation=None) -> bool:
        if not popularity_allows(document,self.settings.min_video_views,self.settings.min_video_comments):
            self.store.record_view(document.ref,"播放量或评论数未严格超过推荐门槛，或统计缺失",now)
            return False
        if self.unfiltered_private_task(task):
            return self._private_task_result(document,task,now)
        binding = self.store.binding(self.settings.owner_person_id, document.ref.provider)
        owner = bool(binding and document.author_id == binding["external_id"])
        local = document_topic(document)
        if blocked(local,owner):
            self.store.record_view(document.ref,"话题限制：跳过严肃或敏感内容",now)
            return False
        if task and task.get("route",{}).get("mode")=="private":
            return self._private_task_result(document, task, now)
        # This payload is deliberately independent of the original private message.
        # Capture before preferences are read, so concurrent forgetting/correction
        # invalidates the whole decision, not just the eventual recommendation.
        epoch = owner_observation["memory_epoch"] if owner_observation else self.store.get_setting("memory_epoch",0)
        readable = replace(document, comments=tuple(c for c in document.comments if c.get("author_id") != self.settings.account_id))
        result, options = self.message_decision("evaluate_video", {
            "persona": self.persona.prompt(), "content": readable.public_context(),
            "interests": list(self.settings.interests), "owner_interests": list(self.settings.owner_interests),
            "owner_preferences": [row for row in self.store.memory(self.settings.owner_person_id, "person", "owner_video_recommendation", now) if row["kind"] == "preference"],
            "requested_topic": task["topic"] if task else owner_observation["topic"] if owner_observation else None,
            "memory_epoch": epoch,
            "max_characters": self.settings.max_recommendation_characters if task else self.settings.max_comment_characters,
            "comment_videos": self.settings.comment_videos and not owner_observation and not self.settings.recommendation_only, "reply_comments": self.settings.reply_comments and task is None and not owner_observation and not self.settings.recommendation_only,
            "recommend_owner": self.settings.recommend_owner,
            "topic_owner_exception": owner,
        }, document.ref.provider, self.settings.account_id, "public", now)
        decision = result.get("decision", "skip")
        topic = combined_topic(local,result.get("topic_class"))
        if blocked(topic,owner):
            self.store.record_view(document.ref,"话题限制：模型跳过或分类缺失",now)
            return False
        document = replace(document,topic_class=topic)
        readable = replace(readable,topic_class=topic)
        if decision not in {"skip", "comment", "reply", "recommend"}:
            raise PolicyError("视频行为选择无效")
        reason = str(result.get("reason", ""))[:200]
        self.store.record_view(document.ref, reason or decision, now)
        if owner_observation and decision not in {"recommend","skip"}:
            return False
        if not owner_observation and not (task and task.get("route")):
            self._queue_reactions(document, now, result.get("interested") is True,memory_epoch=result["_memory_epoch"])
        extra = result.get("comment_reply")
        if not self.settings.recommendation_only and not owner_observation and task is None and decision != "reply" and self.settings.reply_comments and isinstance(extra, dict) and extra.get("reply_to"):
            try:
                self._queue_browse_reply(readable, extra, options, now,memory_epoch=result["_memory_epoch"])
            except PolicyError:
                self.store.audit("browse_reply_skipped", "额外接话目标或正文无效，未生成评论区回复", now)
        if decision == "skip":
            return False
        if self.settings.recommendation_only and decision in {"comment", "reply"}:
            return False
        if decision == "recommend" and not popularity_allows(document,self.settings.min_video_views,self.settings.min_video_comments):
            self.store.audit("recommendation_skipped", "视频播放量或评论数未严格超过门槛，或统计缺失", now)
            return False
        if task and decision != "recommend":
            return False
        if decision == "reply":
            return self._queue_browse_reply(readable, result, options, now,memory_epoch=result["_memory_epoch"]) if self.settings.reply_comments else False
        if decision == "comment" and not self.settings.comment_videos or decision == "recommend" and not self.settings.recommend_owner:
            return False
        if decision == "recommend" and (not binding or binding["external_id"] == document.author_id):
            return False
        limit = self.settings.max_recommendation_characters if decision == "recommend" else self.settings.max_comment_characters
        text, emojis = self.message_text(result, options, limit)
        reply_to, root = "", ""
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
            emojis=emojis, proactive=True,
            topic_class=topic,
            owner_activity_version=owner_observation["version"] if owner_observation else "",
            owner_activity_sources=owner_observation["sources"] if owner_observation else (),
            memory_epoch=result["_memory_epoch"],
        )
        if owner_observation and self.owner_activity and self.owner_activity.guard(action,now):
            return False
        return self.enqueue_generated(action,result,now)

    def _private_task_result(self, document, task, now):
        if not popularity_allows(document,self.settings.min_video_views,self.settings.min_video_comments):
            return False
        route = task["route"]
        binding = self.store.binding(task["person_id"],route["platform"])
        accounts = dict(self.routing()) if self.routing else {self.settings.platform:self.settings.account_id}
        account = accounts.get(route["platform"])
        access = self.private_access.get(route["platform"])
        if (not binding or binding["version"]!=route["binding_version"] or not account or not access
                or not access.allows_private_reply(account,binding["external_id"])):
            raise PolicyError("委托结果的私聊权限或机器人账号不可用")
        if route.get("account_id") and route["account_id"]!=account:
            raise PolicyError("委托结果的机器人账号已变化")
        url = self.content_urls.get(document.ref.provider,lambda ref:document.url)(document.ref)
        if not url:
            raise PolicyError("未取得可信的视频链接")
        epoch = self.store.get_setting("memory_epoch",0)
        result, options = self.message_decision("private_task_result", {
            "persona":self.persona.prompt(True,private=True),"content":document.public_context(),
            "video_url":url,"requested_topic":task["topic"],"memory_epoch":epoch,
            "max_characters":self.settings.max_private_characters},route["platform"],account,"private",now)
        if result.get("decision")=="skip":
            return False
        if result.get("decision")!="recommend":
            raise PolicyError("视频推荐判断无效")
        text = result.get("text")
        if (not isinstance(text,str) or len(text)>self.settings.max_private_characters
                or text.count(url)!=1 or result.get("emojis",[])):
            raise PolicyError("推荐正文必须且只能包含选定视频的可信链接，不附加表情")
        checked_text(text.replace(url,""),self.settings.max_private_characters,
            multilingual=self.private_policies.get(route["platform"],{}).get("multilingual",False))
        text,emojis = text.strip(), ()
        action=Action(stable_id(task["task_id"],task["version"],document.ref.key,task["attempts"]),
            self.settings.agent_id,route["platform"],account,document.ref,text,now,
            min(task["expires_at"],now+self.settings.action_ttl_minutes*60),kind="task_result",
            event_id=task["event_id"],task_id=task["task_id"],task_version=task["version"],
            binding_version=binding["version"],subject_person_id=task["person_id"],
            recipient_id=binding["external_id"],conversation_id=route["conversation_id"],visibility="private",
            memory_epoch=result["_memory_epoch"],evidence_level=document.evidence_level,emojis=emojis,
            dedup_key=stable_id("task-result",task["task_id"],task["version"],document.ref.key))
        return self.enqueue_generated(action,result,now)

    def _queue_browse_reply(self, document, result, options, now, *, memory_epoch: int):
        if self.settings.recommendation_only:
            return False
        target = next((c for c in document.comments if c.get("id") == str(result.get("reply_to", "")) and c.get("author_id") != self.settings.account_id), None)
        if target is None:
            raise PolicyError("只能回复实际读取的其他用户评论")
        binding = self.store.binding(self.settings.owner_person_id,document.ref.provider)
        owner = bool(binding and target.get("author_id") == binding["external_id"])
        topic = combined_topic(document_topic(document,target.get("text","")),result.get("topic_class"))
        if blocked(topic,owner):
            return False
        text, emojis = self.message_text(result, options, self.settings.max_comment_characters)
        root = target.get("root") or target["id"]
        action = Action(stable_id(self.settings.agent_id, self.settings.account_id, "browse_reply", document.ref.key, target["id"]),
                        self.settings.agent_id, document.ref.provider, self.settings.account_id, document.ref, text, now,
                        now+self.settings.action_ttl_minutes*60, kind="browse_reply", reply_to=target["id"], thread_root=root,
                        evidence_level=document.evidence_level, emojis=emojis, proactive=True, topic_class=topic,
                        conversation_id=f"{document.ref.provider}:{self.settings.account_id}:{document.ref.key}:thread:{root}",
                        decision_reason="主动回应已读取的评论", dedup_key=f"browse_reply:{document.ref.key}:{target['id']}",memory_epoch=memory_epoch)
        return self.enqueue_generated(action,result,now)

    def review_one(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        if not self.settings.model_review or self.settings.draft or self.paused():
            return False
        action = self.store.claim_review_action(now,strategy=self.settings.review_strategy, **({"routes": self.routing()} if self.routing else {}))
        if action is None:
            return False
        if self.settings.review_strategy=="inline":
            self.store.record_action_review(action.action_id,"unavailable","旧队列缺少同次判断，请人工审核或重新生成",now,method="inline")
        else:
            self._review_action(action, now)
        return True

    def dispatch_one(self, now: float | None = None, *, pre_reviewed_only: bool = False) -> bool:
        live_clock = now is None
        now = time.time() if now is None else now
        if self.paused():
            return False
        action = self.store.claim_action(now, self.settings.daily_comments, self.settings.daily_recommendations,
                                         day_start(now, self.settings.timezone), self.settings.min_send_gap_seconds,
                                         self.settings.recommendation_gap_minutes * 60,
                                         self.settings.browsing_enabled and next_window(now, self.settings.timezone, self.settings.windows, self.settings.weekdays) == now,
                                         manual_only=self.settings.draft, require_review=self.settings.model_review and pre_reviewed_only,
                                         required_review_method=self.settings.review_strategy if self.settings.model_review and pre_reviewed_only else "",
                                         autonomous_kinds=self.autonomy.allowed_kinds(now),
                                         task_window_open=next_window(now,self.settings.timezone,self.settings.windows,self.settings.weekdays)==now,
                                         **({"routes": self.routing()} if self.routing else {}))
        if action is None:
            return False
        # Read the clock again after any preceding slow operation; test clocks remain deterministic.
        reason = self.store.validate_action(action, now)
        reason = reason or self.action_policy(action, now) or self.autonomy.guard(action, now) or self.social.guard(action,now)
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
            if action.kind in {"private_reply", "proactive_private", "task_result"}:
                access = self.private_access.get(action.platform)
                if access is None or not access.allows_private_reply(action.account_id, action.recipient_id):
                    self.store.finish_action(action.action_id, Receipt("failed", detail="发送前不满足私信回复关系限制"), now)
                    return True
            if self.settings.model_review and not action.approved_manually and action.kind not in {"like", "coin"} and not review_satisfied(action,self.settings.review_strategy):
                if self.settings.review_strategy=="inline":
                    self.store.record_action_review(action.action_id,"unavailable","旧队列缺少同次判断，请人工审核或重新生成",now,method="inline")
                    return True
                reviewed = self._review_action(action, now)
                if reviewed is None:
                    return True
                action = reviewed
            input_waiting = False
            def before_write():
                nonlocal input_waiting
                if action.task_id and self.store.task_input_pending(action):
                    input_waiting = True
                    return False
                moment = time.time() if live_clock else now
                local_ok = not self.store.validate_action(action, moment) and not self.paused(action.platform) and not self.action_policy(action, moment) and not self.autonomy.guard(action, moment) and not self.social.guard(action,moment)
                if not local_ok:
                    return False
                if action.kind in {"private_reply", "proactive_private", "task_result"}:
                    access = self.private_access.get(action.platform)
                    return access is not None and access.allows_private_reply(action.account_id, action.recipient_id)
                return True
            receipt = sink.send(action, mentions, before_write=before_write)
            if input_waiting and receipt.state=="failed":
                self.store.defer_action(action.action_id,now+1,"主人有新消息，等待处理后再投递委托结果")
                return True
        except PlatformError as exc:
            if exc.pause:
                self.pause_platform(exc, action.platform)
            elif exc.code in {"read_deferred","read_limited","-509"}:
                self.store.defer_action(action.action_id,max(now+1,exc.retry_at or now+60),"发送前读取暂缓，等待通道恢复；未执行写入")
                return True
            receipt = Receipt("failed", detail=str(exc))
        except Exception as exc:
            # Transport-level implementation must distinguish definite failures. Otherwise conservative.
            receipt = Receipt("unknown", detail=f"发送未能确认：{type(exc).__name__}")
        if receipt.pause:
            self.store.set_setting(f"platform_paused:{action.platform}", True)
            self.store.set_setting(f"platform_pause_reason:{action.platform}",{"code":"write_risk","at":now})
            self.store.audit("platform_paused",f"{action.platform}: 写入回执要求暂停",now)
        self.store.finish_action(action.action_id, receipt, now)
        return True

    def _review_action(self, action: Action, now: float) -> Action | None:
        event = self.store.event(action.event_id) if action.event_id else None
        document = self.store.cached_document(action.content, now) if action.content else None
        # Other speakers can update the cache for the same video concurrently.
        # A review must use this action's target, never another speaker's chain.
        if action.content and action.reply_to and (not document or not document.comment_context or
                                                   document.comment_context.target_id != action.reply_to):
            document = self.document(action.content, now, reply_to=action.reply_to, root=action.thread_root,
                                     context=event.share_content if event else None)
        owner = bool(self.settings.owner_person_id in action.mentions or action.subject_person_id == self.settings.owner_person_id or
                     (event and self.store.resolve_person(event.platform, event.actor_id) == self.settings.owner_person_id))
        try:
            result = self.model.decide("review_action", {
                "persona": self.persona.prompt(owner and action.visibility != "group", private=action.visibility == "private"), "proposed_text": action.text,
                "channel": "private" if action.visibility == "private" else "public", "action_kind": action.kind,
                "private_policy":dict(self.private_policies.get(action.platform,{})) if action.visibility=="private" else {},
                "owner_addressed": owner, "source_message": event.text if event and (event.visibility != "private" or action.visibility == "private") else None,
                "owner_activity": (self.owner_activity.review_context(action,now)
                                   if action.visibility == "private" and action.owner_activity_version and self.owner_activity else None),
                "content": document.public_context() if document else None,
                    "social_context":self.social.review_context(action,now) if action.social_refs else None,
                "evidence_level": action.evidence_level,
                "emojis": [{"key": e.key, "token": e.token, "meaning": e.label, "kind":e.kind} for e in action.emojis],
                "target_comment": next((c for c in document.comments if c.get("id") == action.reply_to), None) if document and action.reply_to else None,
                "artwork_metadata": {k: action.artwork[k] for k in ("title", "artist_name", "tags") if k in action.artwork},
                "intent_result": self.store.get_setting(f"private_intent:{action.event_id}") if action.visibility == "private" else None,
            })
            decision = result.get("decision")
            if decision not in {"approve", "reject"}:
                raise ModelError("审核未返回有效的 approve/reject 决策")
            reason = str(result.get("reason") or ("审核通过" if decision == "approve" else "需要人工复核"))[:240]
            state = "passed" if decision == "approve" else "rejected"
        except (ModelError, ValueError, TypeError, KeyError):
            state, reason = "unavailable", "模型审核未完成，转人工审核"
        self.store.record_action_review(action.action_id, state, reason, now)
        return replace(action, review_state=state, review_reason=reason,review_method="independent") if state == "passed" else None
