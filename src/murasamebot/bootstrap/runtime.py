from __future__ import annotations

from dataclasses import dataclass, replace
import logging
import time

from murasamebot.adapters.characters.lingchat import load_persona
from murasamebot.adapters.models.chat_completions import ChatCompletionsModel
from murasamebot.adapters.network import rules_for
from murasamebot.adapters.media.http_images import HTTPImageReader
from murasamebot.adapters.persistence.sqlite import SQLiteStore
from murasamebot.application.engine import SocialEngine
from murasamebot.application.artwork import ArtDiscovery, ArtSettings
from murasamebot.application.vision import VisionService, VisionSettings
from murasamebot.application.expressions import ExpressionService, ExpressionSettings
from murasamebot.application.memory import MemoryService, MemorySettings
from murasamebot.application.review import review_satisfied
from murasamebot.bootstrap.config import Config
from murasamebot.bootstrap.platforms import build_modules, close_resources, module_resources, prepare_platform_state
from murasamebot.domain.models import Action, PlatformError, stable_id
from murasamebot.domain.topics import blocked, document_topic, local_topic
from murasamebot.application.scheduling import next_window
from murasamebot.application.request_policy import RequestSettings


LOG = logging.getLogger("murasame")


def prepare_store(config: Config) -> SQLiteStore:
    store = SQLiteStore(config.file("database"), config.data["agent_id"])
    prepare_platform_state(config, store)
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
    platform: object | None
    model: ChatCompletionsModel
    engine: SocialEngine
    artwork: ArtDiscovery | None = None
    resources: tuple = ()
    owner_history: object = None
    network_monitor: object = None

    modules: dict | None = None

    def __post_init__(self):
        # Preserve Runtime(config, store, bili_adapter, model, engine) for callers.
        if self.modules is None:
            self.modules = {}
            if self.platform is not None:
                from murasamebot.bootstrap.platforms.bilibili import BilibiliModule, CAPABILITIES
                self.modules["bilibili"] = BilibiliModule("bilibili", self.platform, CAPABILITIES,
                    history=self.owner_history, artwork=self.artwork, monitor=self.network_monitor)

    def require_platform(self, name):
        module = self.modules.get(name)
        if module is None:
            raise ValueError("平台尚未启用或接入：" + name)
        return module.adapter

    def poll(self, now=None):
        now = time.time() if now is None else now
        for module in self.modules.values():
            if getattr(module.adapter, "configured", True) and not self.engine.paused(module.name):
                try:
                    module.poll(self, now)
                except PlatformError as exc:
                    self.engine.pause_platform(exc, module.name)
                    self.store.audit("platform_poll_error", module.name + ": " + exc.code, now)

    def poll_private(self, now=None):
        now = time.time() if now is None else now
        for module in self.modules.values():
            if getattr(module.adapter, "configured", True) and not self.engine.paused(module.name):
                try:
                    module.poll_private(self, now)
                except PlatformError as exc:
                    self.engine.pause_platform(exc, module.name)
                    self.store.audit("platform_poll_error", module.name + ": " + exc.code, now)

    def ingest(self, event, *, baseline=False):
        if not event.event_id or not event.actor_id or not event.conversation_id or not event.account_id:
            raise ValueError("事件缺少稳定身份或会话标识")
        module = self.modules.get(event.platform)
        if module is None or (module.account_id and module.account_id != event.account_id):
            raise ValueError("事件平台或机器人账号与启用模块不匹配")
        return self.engine.ingest(module.normalize_event(event), baseline=baseline)

    def run_artwork(self, now):
        for module in self.modules.values():
            if not self.engine.paused(module.name):
                module.background(self, now)

    def run_background(self, now):
        # Independent chat initiative must not wait for a content-platform read gate.
        self.engine.autonomy.run(now,platforms=tuple(name for name in self.modules if name!=self.engine.settings.platform))
        # The exploration monitor is specific to the primary content platform.
        if self.network_monitor and not self.network_monitor.allowed("exploration", now):
            return False
        if self.owner_history:
            self.owner_history.recommend(self.engine, now)
        primary = self.modules.get(self.engine.settings.platform)
        if primary and {"autonomous_post", "proactive_private"} & primary.capabilities:
            self.engine.autonomy.run(now,platforms=(primary.name,))
        if primary and "exploration" in primary.capabilities:
            if not self.engine.run_due_task():
                self.engine.browse()
        self.run_artwork(now)
        return False

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
            primary = self.modules.get(self.engine.settings.platform)
            if primary and "exploration" in primary.capabilities:
                if not self.engine.run_due_task():
                    self.engine.browse()
            if self.owner_history:
                self.owner_history.sync(time.time())
                self.owner_history.recommend(self.engine,time.time())
            primary = self.modules.get(self.engine.settings.platform)
            if any({"autonomous_post", "proactive_private"} & m.capabilities for m in self.modules.values()):
                self.engine.autonomy.run(time.time())
            self.run_artwork(time.time())
            self.engine.dispatch_one()
            self.engine.memory_service.maintain()
            self.store.set_setting("last_tick", time.time())
        finally:
            self.store.release_lease("worker_tick")

    def close(self):
        resources = [self.model, *self.resources]
        for module in self.modules.values():
            resources.extend(module_resources(module))
        close_resources(resources)


def build(config: Config, *, concurrency=None, platform_factories=None) -> Runtime:
    reload_config = getattr(config, "runtime_reload", lambda: Config(config.path))
    store = prepare_store(config)
    modules = build_modules(config, store, platform_factories)
    model, resources = None, []
    try:
        primary = modules.get(config.primary_platform)
        platform = primary.adapter if primary else None
        history = primary.history if primary else None
        monitor = primary.monitor if primary else None
        def current_history(current):
            if primary is None or primary.history is None:
                return None
            return primary.current_history(current, store)
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
        reader = HTTPImageReader(hosts=tuple(v.get("allowed_image_hosts", ["i0.hdslb.com", "i1.hdslb.com", "i2.hdslb.com", "bimp.hdslb.com"])), proxy_url=config.network_proxy)
        # Keep the legacy resources view while closing shared references only once.
        resources = [reader, *[m.history_reader for m in modules.values() if m.history_reader is not None]]
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
                        kaomoji_enabled=e.get("kaomoji_enabled",True)), {name: module.adapter for name, module in modules.items() if "emoji" in module.capabilities}, store)
        def action_policy(action: Action, now: float) -> str:
            current = reload_config()
            module = modules.get(action.platform)
            if module is None or action.platform not in current.enabled_platforms:
                return "目标平台未启用或尚未接入"
            if module.account_id and action.account_id != module.account_id:
                return "目标机器人账号已变化"
            if action.kind not in module.capabilities:
                return "目标平台不支持该动作"
            platform_reason = module.guard(current, action, now)
            if platform_reason:
                return platform_reason
            if action.social_refs:
                from murasamebot.application.social import SocialService, SocialSettings
                reason = SocialService(store,SocialSettings(**current.data["social"]),owner_person_id=current.data["owner_person_id"],
                                       observations=current_history(current)).guard(action,now)
                if reason:
                    return reason
            if action.visibility == "public" and action.content:
                event = store.event(action.event_id) if action.event_id else None
                document = store.cached_document(action.content,now)
                binding = store.binding(current.data["owner_person_id"],action.platform)
                owner = bool(binding and ((event and event.actor_id == binding["external_id"])
                    or (not action.reply_to and document and document.author_id == binding["external_id"])
                    or (action.reply_to and document and any(c.get("id")==action.reply_to and c.get("author_id")==binding["external_id"] for c in document.comments))))
                if (blocked(action.topic_class,owner) or blocked(document_topic(document,event.text if event else ""),owner)
                        or local_topic(action.text)=="sensitive"):
                    return "公开话题限制：敏感、非主人严肃话题或未完成分类；人工批准也不放行"
            if action.content and action.content.kind == "video" and action.kind in {"recommendation", "task_result", "browse_comment", "browse_reply", "owner_comment", "reply"}:
                from murasamebot.application.recommendation_policy import popularity_allows
                browse = current.data["browsing"]
                document = store.cached_document(action.content,now) if action.content else None
                source_event = store.event(action.event_id) if action.event_id else None
                explicit_mention = (action.kind == "reply" and action.visibility == "public" and source_event and source_event.kind == "mention"
                    and source_event.visibility == "public" and source_event.platform == action.platform
                    and source_event.account_id == action.account_id and source_event.content == action.content)
                if not explicit_mention and not popularity_allows(document,browse.get("min_video_views",8000),browse.get("min_video_comments",20)):
                    return "视频统计缺失或未超过当前推荐门槛，禁止推荐"
            if action.owner_activity_version:
                active_history = current_history(current)
                reason = active_history.guard(action,now) if active_history else "主人观察服务不可用"
                if reason:
                    return reason
            if action.kind in {"autonomous_post", "proactive_private"}:
                from murasamebot.application.autonomy import AutonomySettings
                policy = AutonomySettings.from_config(current.data[module.public_autonomy_key if action.kind=="autonomous_post" and module.public_autonomy_key else module.autonomy_key])
                channel = "private" if action.kind == "proactive_private" else "dynamic"
                if (not policy.enabled(channel) or policy.version() != action.autonomy_version
                        or policy.open_at(now, current.data["timezone"]) != now):
                    return "自主发布设置已变化或不在允许时段"
            if (current.data["mode"]=="reviewed" and current.data["review"]["strategy"]!=config.data["review"]["strategy"]
                    and not action.approved_manually and action.kind not in {"like","coin"}
                    and not review_satisfied(action,current.data["review"]["strategy"])):
                return "审核策略已经变化，需要按当前策略重新审核"
            if action.emojis and (not current.data.get("expressions", {}).get("enabled", True) or len(action.emojis) > current.data.get("expressions", {}).get("max_per_message", 2)):
                return "表情设置已变化，请重新生成或取消此草稿"
            if any(e.kind=="kaomoji" for e in action.emojis) and not current.data.get("expressions",{}).get("kaomoji_enabled",True):
                return "颜文字已关闭，请重新生成或取消此草稿"
            if action.task_id and (task := store.task(action.task_id)) and task.get("route"):
                if next_window(now,current.data["timezone"],tuple((w["start"],w["end"]) for w in current.data["browsing"]["windows"]),tuple(current.data["browsing"]["weekdays"])) != now:
                    return "委托投递不在配置时段"
            if action.kind == "artwork_share":
                art = ArtSettings.from_config(current.data.get("artwork", {}))
                if not art.enabled or art.delivery != action.artwork.get("delivery"):
                    return "美图分享已关闭或投递方式已改变"
                if action.artwork.get("delivery") == "image":
                    grant = art.permission(action.artwork, now)
                    if not grant or stable_id(grant) != action.artwork.get("permission_id"):
                        return "作品转载授权已经撤销、到期或变化"
            return ""
        behaviour = config.behaviour(primary.account_id if primary else "")
        capabilities = primary.capabilities if primary else frozenset()
        behaviour = replace(behaviour,
            browsing_enabled=behaviour.browsing_enabled and "exploration" in capabilities,
            private_messages_enabled=behaviour.private_messages_enabled and "private_reply" in capabilities,
            autonomy=replace(behaviour.autonomy,
                dynamic_enabled=behaviour.autonomy.dynamic_enabled and "autonomous_post" in capabilities,
                private_enabled=behaviour.autonomy.private_enabled and "proactive_private" in capabilities))
        engine = SocialEngine(behaviour, store, model, persona,
                               {name: module.adapter for name, module in modules.items() if "content" in module.capabilities},
                               {name: module.adapter for name, module in modules.items()}, action_policy=action_policy,
                               private_access={name: module.adapter for name, module in modules.items() if "private_reply" in module.capabilities}, vision=vision, expressions=expressions, owner_activity=history,
                               memory=MemoryService(store,model,MemorySettings(**config.data.get("memory",{})),timezone=config.data["timezone"],
                                                    observations=history,persona=persona.prompt(True,private=True),
                                                    observation_guard=lambda context,now: bool((h := current_history(reload_config())) and h.diary_valid(context,now))))
        def active_routes():
            enabled = reload_config().enabled_platforms
            return tuple((name, module.account_id) for name, module in modules.items() if name in enabled)
        engine.routing = active_routes
        if 'qq' in modules:
            from murasamebot.application.group_chat import GroupChat
            from murasamebot.adapters.persistence.group_inbox import GroupInbox
            group_chat=GroupChat(engine,config.data['qq_groups'],GroupInbox(store),modules['qq'].adapter)
            from murasamebot.application.group_learning import GroupLearning
            from murasamebot.adapters.persistence.group_learning import GroupKnowledge
            knowledge=GroupKnowledge(store)
            def learning_current():
                current=reload_config()
                groups=dict(current.data['qq_groups'])
                if 'qq' not in current.enabled_platforms:groups['enabled']=False
                return groups,current.data['qq_learning'],current.data['qq']['robot_id']
            engine.group_learning=GroupLearning(store,model,knowledge,learning_current)
            group_chat.knowledge=lambda account,room,text: knowledge.context(account,room,text,learning_current()[1]) if room in learning_current()[0]['group_ids'] and learning_current()[0]['enabled'] else []
            engine.event_handlers.update(group_message=group_chat.handle,group_window=group_chat.handle)
        from murasamebot.application.autonomy import AutonomySettings, AutonomyTarget
        engine.autonomy.targets = tuple(AutonomyTarget(name,module.account_id,
            replace(AutonomySettings.from_config(config.data[module.autonomy_key]),
                dynamic_enabled=config.data[module.autonomy_key]["dynamic_enabled"] and "autonomous_post" in module.capabilities,
                private_enabled=config.data[module.autonomy_key]["private_enabled"] and "proactive_private" in module.capabilities),
            module.autonomy_private_enabled,module.autonomy_activity,name if module.autonomy_key!="autonomy" else "",
            ("private",) if module.public_autonomy_key else ("dynamic","private"))
            for name,module in modules.items() if {"autonomous_post","proactive_private"} & module.capabilities)
        engine.autonomy.targets += tuple(AutonomyTarget(name,module.account_id,
            AutonomySettings.from_config(config.data[module.public_autonomy_key]),scope=name+":space",channels=("dynamic",))
            for name,module in modules.items() if module.public_autonomy_key)
        from murasamebot.application.task_routing import owner_video_route
        engine.request_route = lambda event,person: owner_video_route(store,event,person,provider="bilibili",
            private_platform="qq",public_platform="bilibili",accounts=dict(active_routes())) if event.platform=="qq" else None
        engine.content_urls["bilibili"] = lambda ref: "https://www.bilibili.com/video/av"+ref.external_id if ref.kind=="video" and ref.external_id.isdigit() else ""

        engine.memory_service.shared_owner_private = "qq" in modules
        engine.private_policies = {name:dict(module.private_policy) for name,module in modules.items() if module.private_policy}
        engine.private_content_providers = {name:module.private_content_provider for name,module in modules.items() if module.private_content_provider}
        for name, module in modules.items():
            if getattr(module, "private_image_reader", None):
                engine.private_visions[name] = VisionService(vision_settings, module.private_image_reader,
                    vision_model, store, model_name=v.get("name") or m["name"])

        for module in modules.values():
            if module.artwork:
                module.artwork.model, module.artwork.persona = model, persona
        artwork = primary.artwork if primary else None
        return Runtime(config, store, platform, model, engine, artwork, tuple(resources), history, monitor, modules)
    except BaseException:
        close_resources([model, *resources, *[r for module in modules.values() for r in module_resources(module)]])
        raise



def readiness(runtime: Runtime) -> dict:
    statuses = {name: module.status(runtime) for name, module in runtime.modules.items()}
    primary = statuses.get(runtime.engine.settings.platform, {})
    return {
        "platforms": statuses, "primary_platform": runtime.engine.settings.platform,
        "requests": runtime.store.request_status(time.time(),RequestSettings(**runtime.config.data["requests"])),
        "memory": {**runtime.store.memory_status(),"health":runtime.store.get_setting("memory_health",{}),
                   "last_contexts":[row for row in runtime.store.memory_contexts()]},
        "autonomy": {c:runtime.store.get_setting("autonomy_status:"+c) for c in ("dynamic","private")},
        "language": "Python 3.12+", "agent_id": runtime.config.data["agent_id"],
        "persona": runtime.engine.persona.name, "persona_version": runtime.engine.persona.version[:12],
        "source_changed": runtime.engine.persona.source_changed,
        "text_language": "中文", "mode": runtime.config.data["mode"],
        "model_configured": runtime.model.configured,
        "login_configured": bool(statuses) and all(s["configured"] for s in statuses.values()),
        "owner_uid": (runtime.store.binding(runtime.config.data["owner_person_id"], runtime.engine.settings.platform) or {}).get("external_id"),
        "browsing_enabled": runtime.engine.settings.browsing_enabled,
        "paused": runtime.engine.paused(runtime.engine.settings.platform), "last_tick": runtime.store.get_setting("last_tick"),
        "live_verified": False,
        "proxy_enabled": bool(runtime.config.proxy_url),
        "proxy_mode": runtime.config.data["network"]["proxy_mode"],
        "proxy_rules": rules_for(runtime.config.network_proxy).status(),
        "artwork_enabled": bool(runtime.artwork and runtime.artwork.settings.enabled),
        "private_messages_enabled": primary.get("private_messages_enabled", runtime.engine.settings.private_messages_enabled),
        "private_reply_policy": primary.get("private_reply_policy", "unavailable"),
        "reply_poll_seconds": primary.get("reply_poll_seconds", 15),
        "owner_poll_seconds": primary.get("owner_poll_seconds", 0),
        "vision_enabled": runtime.config.data.get("vision", {}).get("enabled", True),
        "video_analysis": primary.get("video_analysis", "unavailable"),
        "model_concurrency": runtime.config.data.get("processing", {}).get("model_concurrency", 4),
        "inbox_batch_limit": primary.get("inbox_batch_limit", 0),
        "warnings": [w for warnings in runtime.store.get_setting("inbox_warnings", {}).values() for w in warnings],
        "review_strategy": runtime.config.data["review"]["strategy"],
        "queues": runtime.store.queue_status(review_strategy=runtime.config.data["review"]["strategy"],
            **({"routes": runtime.engine.routing()} if runtime.engine.routing else {})),
    }
