from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
import json
import logging
import time

from murasamebot.adapters.characters.lingchat import load_persona
from murasamebot.adapters.artwork.sources import PixivSource, XSource
from murasamebot.adapters.models.chat_completions import ChatCompletionsModel
from murasamebot.adapters.network import rules_for
from murasamebot.adapters.media.http_images import HTTPImageReader
from murasamebot.adapters.persistence.sqlite import SQLiteStore
from murasamebot.adapters.platforms.bilibili import BilibiliAdapter
from murasamebot.application.engine import SocialEngine
from murasamebot.application.artwork import ArtDiscovery, ArtSettings
from murasamebot.application.vision import VisionService, VisionSettings
from murasamebot.application.expressions import ExpressionService, ExpressionSettings
from murasamebot.application.memory import MemoryService, MemorySettings
from murasamebot.application.review import review_satisfied
from murasamebot.bootstrap.config import Config
from murasamebot.domain.models import Action, PlatformError, stable_id


LOG = logging.getLogger("murasame")


def prepare_store(config: Config) -> SQLiteStore:
    store = SQLiteStore(config.file("database"), config.data["agent_id"])
    autonomy_stamp = stable_id(config.data["autonomy"], config.data["bilibili"].get("private_messages_enabled",True))
    previous = store.get_setting("autonomy_config_stamp")
    if previous != autonomy_stamp:
        if previous is not None:
            store.cancel_autonomy()
        store.set_setting("autonomy_config_stamp", autonomy_stamp)
    if config.data["mode"] == "draft":
        store.enable_review_mode()
    stamp = stable_id(config.data.get("bindings", []))
    if store.get_setting("configured_binding_keys") is None and store.get_setting("binding_config_stamp") == stamp:
        store.set_setting("configured_binding_keys", sorted((str(row["person_id"]), str(row["platform"])) for row in config.data.get("bindings", [])))
    # A CLI revocation remains revoked on restart unless the binding configuration changes.
    if store.get_setting("binding_config_stamp") != stamp:
        desired_keys = {(str(row["person_id"]), str(row["platform"])) for row in config.data.get("bindings", [])}
        for previous in store.get_setting("configured_binding_keys", []):
            if tuple(previous) not in desired_keys:
                store.revoke_binding(*previous)
        for row in config.data.get("bindings", []):
            store.bind(str(row["person_id"]), str(row["platform"]), str(row["external_id"]),
                       str(row.get("display_name", "")), str(row.get("purpose", "owner_video_recommendation")))
        store.set_setting("binding_config_stamp", stamp)
        store.set_setting("configured_binding_keys", sorted(desired_keys))
    paused = bool(config.data.get("paused", False))
    if store.get_setting("pause_config_stamp") != paused:
        store.set_setting("paused", paused)
        store.set_setting("pause_config_stamp", paused)
    return store


@dataclass
class Runtime:
    config: Config
    store: SQLiteStore
    platform: BilibiliAdapter
    model: ChatCompletionsModel
    engine: SocialEngine
    artwork: ArtDiscovery | None = None
    resources: tuple = ()

    def record_inbox_warnings(self, channel: str, now: float):
        warnings = getattr(self.platform, "take_warnings", lambda: [])()
        current = self.store.get_setting("inbox_warnings", {})
        previous = current.get(channel, [])
        if warnings != previous:
            for warning in warnings:
                self.store.audit("inbox_warning", warning["detail"], now)
                LOG.warning("收件警告：%s", warning["detail"])
        current[channel] = warnings
        self.store.set_setting("inbox_warnings", current)

    def poll(self, now: float | None = None):
        now = time.time() if now is None else now
        if self.engine.paused("bilibili"):
            return
        try:
            events = self.platform.notifications(now)
            self.record_inbox_warnings("notifications", now)
            self.engine.settings = replace(self.engine.settings, account_id=self.platform.account_id)
            key = f"notifications_initialized:{self.platform.account_id}"
            initialized = self.store.get_setting(key, False)
            added = 0
            for event in events:
                added += self.engine.ingest(event, baseline=not initialized)
            self.store.set_setting(key, True)
            if added or not initialized:
                LOG.info("评论/@通知：%s %d 条", "建立历史基线" if not initialized else "发现新事件", added)
        except PlatformError as exc:
            self.store.audit("notification_error", str(exc), now)
            if exc.pause:
                self.engine.pause_platform(exc, "bilibili")
            LOG.warning("通知读取未完成：%s", exc)
        if self.engine.paused("bilibili"):
            return
        owner = self.store.binding(self.config.data["owner_person_id"], "bilibili")
        if not owner:
            return
        if owner["external_id"] == self.platform.account_id:
            self.store.set_setting("platform_paused:bilibili", True)
            self.store.audit("wrong_account", "主人账号与 AI 账号相同，已暂停以防止对自身内容循环转发", now)
            return
        poll_key = f"owner_poll_at:{self.platform.account_id}:{owner['external_id']}"
        if now - self.store.get_setting(poll_key, 0) < self.config.data["bilibili"].get("poll_seconds", 0):
            return
        self.store.set_setting(poll_key, now)
        try:
            events = self.platform.owner_content(owner["external_id"], now)
            key = f"owner_initialized:{self.platform.account_id}:{owner['external_id']}"
            initialized = self.store.get_setting(key, False)
            since_key = key + ":since"
            since = self.store.get_setting(since_key, now - 2)
            for event in events:
                is_video = event.content.kind == "video"
                old = not initialized or event.occurred_at < since
                controls = self.config.data["bilibili"]
                enabled = controls["owner_new_videos" if is_video else "owner_new_dynamics"]
                self.engine.ingest(event, baseline=old or not enabled or event.kind == "owner_share_only")
                if event.share_content:
                    repost = replace(event, event_id=event.event_id + ":repost", kind="owner_repost",
                                     content=event.share_content, share_content=event.content)
                    repost_enabled = controls.get("repost_owner_videos" if is_video else "repost_owner_dynamics", False)
                    self.engine.ingest(repost, baseline=old or not repost_enabled)
            if not initialized:
                self.store.set_setting(since_key, now - 2)
            self.store.set_setting(key, True)
        except PlatformError as exc:
            self.store.audit("owner_poll_error", str(exc), now)
            if exc.pause:
                self.engine.pause_platform(exc, "bilibili")
            LOG.warning("主人内容监测未完成：%s", exc)

    def tick(self):
        now = time.time()
        if not self.store.acquire_lease("worker_tick", now, 1200):
            return
        try:
            self.poll()
            self.poll_private()
            for _ in range(3):
                processed = self.engine.process_one_event()
                self.engine.dispatch_one()
                if not processed:
                    break
            if not self.engine.run_due_task():
                self.engine.browse()
            self.engine.autonomy.run(time.time())
            if self.artwork:
                self.artwork.run(time.time(), agent_id=self.engine.settings.agent_id, platform="bilibili",
                                 account_id=self.platform.account_id, timezone=self.engine.settings.timezone,
                                 draft=self.engine.settings.draft, ttl_minutes=self.engine.settings.action_ttl_minutes,
                                 review_strategy=self.engine.settings.review_strategy if self.engine.settings.model_review else "none")
            self.engine.dispatch_one()
            self.engine.memory_service.maintain()
            self.store.set_setting("last_tick", time.time())
        finally:
            self.store.release_lease("worker_tick")

    def poll_private(self, now: float | None = None):
        now = time.time() if now is None else now
        if self.engine.paused("bilibili") or not self.config.data["bilibili"].get("private_messages_enabled", True):
            return
        try:
            uid = self.platform.account_id or self.platform.account()["uid"]
            key = f"private_inbox:{uid}"
            checkpoint = self.store.get_setting(key)
            baseline = checkpoint is None
            since = checkpoint["since"] if checkpoint else now
            cursors = dict(checkpoint.get("cursors", {})) if checkpoint else {}
            batch = self.platform.private_messages(now, since=since, cursors=cursors, baseline=baseline)
            self.record_inbox_warnings("private", now)
            self.engine.settings = replace(self.engine.settings, account_id=self.platform.account_id)
            # Persist events before advancing cursors. A crash between the two
            # re-reads duplicates, which enqueue_event safely rejects.
            for event in batch.events:
                self.engine.ingest(event, baseline=baseline)
            cursors.update(batch.cursors)
            self.store.set_setting(key, {"since": since, "cursors": cursors})
            if baseline:
                self.store.audit("private_initialized", "私信历史基线已建立，此后仅处理互关用户的新文字消息", now)
                LOG.info("私信历史基线已建立，仅处理此后的互关新消息")
            if batch.events or batch.denied_peers:
                self.store.audit("private_polled", f"读取新私信 {len(batch.events)} 条；非互关会话略过 {batch.denied_peers} 个", now)
                LOG.info("私信：新消息 %d 条；非互关会话略过 %d 个", len(batch.events), batch.denied_peers)
        except PlatformError as exc:
            self.engine.pause_platform(exc, "bilibili")
            self.store.audit("private_poll_error", str(exc), now)
            LOG.warning("私信读取未完成：%s", exc)

    def close(self):
        self.platform.close()
        self.model.close()
        if self.artwork:
            for source in self.artwork.sources.values():
                source.close()
        for resource in self.resources:
            resource.close()


def build(config: Config, *, concurrency=None) -> Runtime:
    store = prepare_store(config)
    b = config.data["bilibili"]
    platform = BilibiliAdapter((config.root / b["credentials"]).resolve(), expected_uid=str(b.get("robot_uid", "")),
                               pages=b["notification_pages"], max_age_hours=b["notification_max_age_hours"], proxy_url=config.network_proxy,
                               private_session_pages=b.get("private_session_pages", 3), private_message_pages=b.get("private_message_pages", 3),
                               private_max_age_hours=b.get("private_message_max_age_hours", 24),
                               read_video_subtitles=b.get("read_video_subtitles", True), inbox_batch_limit=b.get("inbox_batch_limit", 90),
                               proactive_recipient_id=(store.binding(config.data["owner_person_id"], "bilibili") or {}).get("external_id", ""))
    m = config.data["model"]
    model = ChatCompletionsModel(base_url=m["base_url"], name=m["name"], api_key=m["api_key"], store=store,
                                 timezone=config.data["timezone"], daily_limit=m["daily_call_limit"],
                                 timeout=m["timeout_seconds"], max_context=m["max_context_characters"], proxy_url=config.network_proxy,
                                 max_output_tokens=m.get("max_output_tokens"), concurrency=concurrency)
    persona = load_persona(config.file("character_source"), config.file("character_profile"))
    if persona.source_changed:
        LOG.warning("源人设已变化：请复核 character/text_profile.yml，现有记忆与任务保留")
    v = config.data.get("vision", {})
    vision_settings = VisionSettings(**{key: v[key] for key in VisionSettings.__dataclass_fields__ if key in v})
    reader = HTTPImageReader(hosts=tuple(v.get("allowed_image_hosts", ["i0.hdslb.com", "i1.hdslb.com", "i2.hdslb.com"])), proxy_url=config.network_proxy)
    resources = [reader]
    vision_model = model
    if v.get("base_url") or v.get("name") or v.get("api_key"):
        base = v.get("base_url") or m["base_url"]
        # Never forward the text provider's key to a different vision service.
        key = v.get("api_key") or (m["api_key"] if base.rstrip("/") == m["base_url"].rstrip("/") else "")
        vision_model = ChatCompletionsModel(base_url=base, name=v.get("name") or m["name"], api_key=key, store=store,
                                           timezone=config.data["timezone"], daily_limit=m["daily_call_limit"], timeout=m["timeout_seconds"],
                                           max_context=m["max_context_characters"], proxy_url=config.network_proxy, max_output_tokens=m.get("max_output_tokens"), concurrency=concurrency)
        resources.append(vision_model)
    vision = VisionService(vision_settings, reader, vision_model, store, model_name=v.get("name") or m["name"])
    e = config.data.get("expressions", {})
    expressions = ExpressionService(ExpressionSettings(enabled=e.get("enabled", True), max_per_message=e.get("max_per_message", 2),
                    cache_minutes=e.get("cache_minutes", 360), preferred_keywords=tuple(e.get("preferred_keywords", ["千恋万花", "丛雨"])),
                    kaomoji_enabled=e.get("kaomoji_enabled",True)), {"bilibili": platform}, store)
    def action_policy(action: Action, now: float) -> str:
        current = Config(config.path)
        if action.kind in {"autonomous_post", "proactive_private"}:
            from murasamebot.application.autonomy import AutonomySettings
            policy = AutonomySettings.from_config(current.data["autonomy"])
            channel = "private" if action.kind == "proactive_private" else "dynamic"
            if (not policy.enabled(channel) or policy.version() != action.autonomy_version
                    or policy.open_at(now, current.data["timezone"]) != now):
                return "自主发布设置已变化或不在允许时段"
        if (current.data["mode"]=="reviewed" and current.data["review"]["strategy"]!=config.data["review"]["strategy"]
                and not action.approved_manually and action.kind not in {"like","coin"}
                and not review_satisfied(action,current.data["review"]["strategy"])):
            return "审核策略已经变化，需要按当前策略重新审核"
        if action.kind in {"browse_comment", "browse_reply", "recommendation"}:
            key = {"browse_comment": "comment_videos", "browse_reply": "reply_comments", "recommendation": "recommend_owner"}[action.kind]
            if not current.data["browsing"].get(key, True):
                return "对应主动评论、接话或推荐功能已关闭"
        if action.emojis and (not current.data.get("expressions", {}).get("enabled", True) or len(action.emojis) > current.data.get("expressions", {}).get("max_per_message", 2)):
            return "表情设置已变化，请重新生成或取消此草稿"
        if any(e.kind=="kaomoji" for e in action.emojis) and not current.data.get("expressions",{}).get("kaomoji_enabled",True):
            return "颜文字已关闭，请重新生成或取消此草稿"
        if action.kind in {"like", "coin"}:
            key = "video_coin" if action.kind == "coin" else "video_like" if action.content and action.content.kind == "video" else "dynamic_like"
            if not current.data.get("engagement", {}).get(key, False):
                return "对应点赞或投币功能已关闭"
            if action.proactive and not current.data["browsing"]["enabled"]:
                return "自主浏览已关闭"
        if action.kind in {"private_reply", "proactive_private"} and not current.data["bilibili"].get("private_messages_enabled", True):
            return "私信回复功能已关闭"
        if action.kind == "artwork_share":
            art = ArtSettings.from_config(current.data.get("artwork", {}))
            if not art.enabled or art.delivery != action.artwork.get("delivery"):
                return "美图分享已关闭或投递方式已改变"
            if action.artwork.get("delivery") == "image":
                grant = art.permission(action.artwork, now)
                if not grant or stable_id(grant) != action.artwork.get("permission_id"):
                    return "作品转载授权已经撤销、到期或变化"
        if action.kind == "owner_repost":
            key = "repost_owner_videos" if action.source_kind == "video" else "repost_owner_dynamics"
            if not current.data["bilibili"].get(key, False):
                return "主人转发功能已关闭"
        return ""
    engine = SocialEngine(config.behaviour(platform.account_id), store, model, persona,
                           {"bilibili": platform}, {"bilibili": platform}, action_policy=action_policy,
                           private_access={"bilibili": platform}, vision=vision, expressions=expressions,
                           memory=MemoryService(store,model,MemorySettings(**config.data.get("memory",{})),timezone=config.data["timezone"]))
    a = config.data.get("artwork", {})
    sources = {}
    for source in a.get("sources", []):
        sources[source] = PixivSource(a.get("pixiv_session", ""), proxy_url=config.network_proxy) if source == "pixiv" else XSource(a.get("x_bearer_token", ""), proxy_url=config.network_proxy)
    artwork = ArtDiscovery(ArtSettings.from_config(a), sources, store, model, persona)
    return Runtime(config, store, platform, model, engine, artwork, tuple(resources))


def readiness(runtime: Runtime) -> dict:
    return {
        "memory": {**runtime.store.memory_status(),"health":runtime.store.get_setting("memory_health",{}),
                   "last_contexts":[row for row in runtime.store.memory_contexts()]},
        "autonomy": {c:runtime.store.get_setting("autonomy_status:"+c) for c in ("dynamic","private")},
        "language": "Python 3.12+", "agent_id": runtime.config.data["agent_id"],
        "persona": runtime.engine.persona.name, "persona_version": runtime.engine.persona.version[:12],
        "source_changed": runtime.engine.persona.source_changed,
        "text_language": "中文", "mode": runtime.config.data["mode"],
        "model_configured": runtime.model.configured,
        "login_configured": runtime.platform.configured,
        "owner_uid": (runtime.store.binding(runtime.config.data["owner_person_id"], "bilibili") or {}).get("external_id"),
        "browsing_enabled": runtime.config.data["browsing"]["enabled"],
        "paused": runtime.engine.paused("bilibili"), "last_tick": runtime.store.get_setting("last_tick"),
        "live_verified": False,
        "proxy_enabled": bool(runtime.config.proxy_url),
        "proxy_mode": runtime.config.data["network"]["proxy_mode"],
        "proxy_rules": rules_for(runtime.config.network_proxy).status(),
        "artwork_enabled": runtime.config.data.get("artwork", {}).get("enabled", False),
        "private_messages_enabled": runtime.config.data["bilibili"].get("private_messages_enabled", True),
        "private_reply_policy": "mutual_follow_only",
        "reply_poll_seconds": runtime.config.data["bilibili"].get("reply_poll_seconds", 15),
        "owner_poll_seconds": runtime.config.data["bilibili"]["poll_seconds"],
        "vision_enabled": runtime.config.data.get("vision", {}).get("enabled", True),
        "video_analysis": "title_description" if not runtime.config.data["bilibili"].get("read_video_subtitles", True) else "title_description_available_subtitles",
        "model_concurrency": runtime.config.data.get("processing", {}).get("model_concurrency", 4),
        "inbox_batch_limit": runtime.config.data["bilibili"].get("inbox_batch_limit", 90),
        "warnings": [w for warnings in runtime.store.get_setting("inbox_warnings", {}).values() for w in warnings],
        "review_strategy": runtime.config.data["review"]["strategy"],
        "queues": runtime.store.queue_status(review_strategy=runtime.config.data["review"]["strategy"]),
    }
