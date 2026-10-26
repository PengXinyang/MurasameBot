from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
import json
import logging
import time

from murasamebot.adapters.characters.lingchat import load_persona
from murasamebot.adapters.artwork.sources import PixivSource, XSource
from murasamebot.adapters.models.chat_completions import ChatCompletionsModel
from murasamebot.adapters.persistence.sqlite import SQLiteStore
from murasamebot.adapters.platforms.bilibili import BilibiliAdapter
from murasamebot.application.engine import SocialEngine
from murasamebot.application.artwork import ArtDiscovery, ArtSettings
from murasamebot.bootstrap.config import Config
from murasamebot.domain.models import Action, PlatformError, stable_id


LOG = logging.getLogger("murasame")


def prepare_store(config: Config) -> SQLiteStore:
    store = SQLiteStore(config.file("database"), config.data["agent_id"])
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

    def poll(self, now: float | None = None):
        now = time.time() if now is None else now
        if self.engine.paused("bilibili"):
            return
        try:
            events = self.platform.notifications(now)
            self.engine.settings = replace(self.engine.settings, account_id=self.platform.account_id)
            key = f"notifications_initialized:{self.platform.account_id}"
            initialized = self.store.get_setting(key, False)
            for event in events:
                self.engine.ingest(event, baseline=not initialized)
            self.store.set_setting(key, True)
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
            for _ in range(3):
                if not self.engine.process_one_event():
                    break
            if not self.engine.run_due_task():
                self.engine.browse()
            if self.artwork:
                self.artwork.run(time.time(), agent_id=self.engine.settings.agent_id, platform="bilibili",
                                 account_id=self.platform.account_id, timezone=self.engine.settings.timezone,
                                 draft=self.engine.settings.draft, ttl_minutes=self.engine.settings.action_ttl_minutes)
            self.engine.dispatch_one()
            self.store.set_setting("last_tick", time.time())
        finally:
            self.store.release_lease("worker_tick")

    def close(self):
        self.platform.close()
        self.model.close()
        if self.artwork:
            for source in self.artwork.sources.values():
                source.close()


def build(config: Config) -> Runtime:
    store = prepare_store(config)
    b = config.data["bilibili"]
    platform = BilibiliAdapter((config.root / b["credentials"]).resolve(), expected_uid=str(b.get("robot_uid", "")),
                               pages=b["notification_pages"], max_age_hours=b["notification_max_age_hours"], proxy_url=config.proxy_url)
    m = config.data["model"]
    model = ChatCompletionsModel(base_url=m["base_url"], name=m["name"], api_key=m["api_key"], store=store,
                                 timezone=config.data["timezone"], daily_limit=m["daily_call_limit"],
                                 timeout=m["timeout_seconds"], max_context=m["max_context_characters"], proxy_url=config.proxy_url)
    persona = load_persona(config.file("character_source"), config.file("character_profile"))
    if persona.source_changed:
        LOG.warning("源人设已变化：请复核 character/text_profile.yml，现有记忆与任务保留")
    def action_policy(action: Action, now: float) -> str:
        current = Config(config.path)
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
                           {"bilibili": platform}, {"bilibili": platform}, action_policy=action_policy)
    a = config.data.get("artwork", {})
    sources = {}
    for source in a.get("sources", []):
        sources[source] = PixivSource(a.get("pixiv_session", ""), proxy_url=config.proxy_url) if source == "pixiv" else XSource(a.get("x_bearer_token", ""), proxy_url=config.proxy_url)
    artwork = ArtDiscovery(ArtSettings.from_config(a), sources, store, model, persona)
    return Runtime(config, store, platform, model, engine, artwork)


def readiness(runtime: Runtime) -> dict:
    return {
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
        "artwork_enabled": runtime.config.data.get("artwork", {}).get("enabled", False),
    }
