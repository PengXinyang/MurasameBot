"""Bilibili-only composition and lifecycle; adapter behavior stays in place."""
from dataclasses import replace
import logging
import time

from murasamebot.bootstrap.platforms import PlatformModule
from murasamebot.domain.models import PlatformError, stable_id

LOG = logging.getLogger("murasame")
CAPABILITIES = frozenset({"reply", "owner_comment", "private_reply", "proactive_private", "autonomous_post",
    "owner_repost", "browse_comment", "browse_reply", "recommendation", "like", "coin",
    "artwork_share", "content", "exploration", "emoji", "owner_history"})


def prepare_state(config, store):
    store.owner_history_prune(config.data["owner_person_id"], time.time(), config.data["owner_history"]["retention_days"])
    # Preserve the existing checkpoint and invalidation semantics during extraction.
    stamp = stable_id(config.data["autonomy"], config.data["bilibili"].get("private_messages_enabled", True))
    previous = store.get_setting("autonomy_config_stamp")
    if previous != stamp:
        if previous is not None:
            store.cancel_autonomy(platform="bilibili")
        store.set_setting("autonomy_config_stamp", stamp)


class BilibiliModule(PlatformModule):
    def normalize_event(self, event):
        # Preserve existing source links, checkpoints and tombstones.
        return event

    def current_history(self, config, store):
        from murasamebot.bootstrap.owner_history import history_service
        return history_service(config, store, self.history_reader)

    def record_inbox_warnings(self, runtime, channel: str, now: float):
        warnings = getattr(self.adapter, "take_warnings", lambda: [])()
        current = runtime.store.get_setting("inbox_warnings", {})
        previous = current.get(channel, [])
        if warnings != previous:
            for warning in warnings:
                runtime.store.audit("inbox_warning", warning["detail"], now)
                LOG.warning("收件警告：%s", warning["detail"])
        current[channel] = warnings
        runtime.store.set_setting("inbox_warnings", current)

    def _poll_notifications(self, runtime, now):
        try:
            events = self.adapter.notifications(now)
            self.record_inbox_warnings(runtime, "notifications", now)
            if runtime.engine.settings.platform == self.name:
                runtime.engine.settings = replace(runtime.engine.settings, account_id=self.adapter.account_id)
            key = f"notifications_initialized:{self.adapter.account_id}"
            initialized = runtime.store.get_setting(key, False)
            added = 0
            for event in events:
                added += runtime.engine.ingest(event, baseline=not initialized)
            runtime.store.set_setting(key, True)
            if added or not initialized:
                LOG.info("评论/@通知：%s %d 条", "建立历史基线" if not initialized else "发现新事件", added)
        except PlatformError as exc:
            runtime.store.audit("notification_error", str(exc), now)
            if exc.pause:
                runtime.engine.pause_platform(exc, "bilibili")
            LOG.warning("通知读取未完成：%s", exc)

    def poll(self, runtime, now: float | None = None):
        now = time.time() if now is None else now
        if runtime.engine.paused("bilibili"):
            return
        if self.monitor is None or (self.monitor.allowed("notifications",now) and
                runtime.store.claim_poll_interval("notifications",now,runtime.config.data["bilibili"]["reply_poll_seconds"])):
            self._poll_notifications(runtime, now)
        if self.monitor and not self.monitor.allowed("owner_feed",now):
            return
        if runtime.engine.paused("bilibili"):
            return
        owner = runtime.store.binding(runtime.config.data["owner_person_id"], "bilibili")
        if not owner:
            return
        if owner["external_id"] == self.adapter.account_id:
            runtime.store.set_setting("platform_paused:bilibili", True)
            runtime.store.audit("wrong_account", "主人账号与 AI 账号相同，已暂停以防止对自身内容循环转发", now)
            return
        poll_key = f"owner_poll_at:{self.adapter.account_id}:{owner['external_id']}"
        if now - runtime.store.get_setting(poll_key, 0) < runtime.config.data["bilibili"].get("poll_seconds", 0):
            return
        runtime.store.set_setting(poll_key, now)
        try:
            events = self.adapter.owner_content(owner["external_id"], now)
            key = f"owner_initialized:{self.adapter.account_id}:{owner['external_id']}"
            initialized = runtime.store.get_setting(key, False)
            since_key = key + ":since"
            since = runtime.store.get_setting(since_key, now - 2)
            for event in events:
                is_video = event.content.kind == "video"
                old = not initialized or event.occurred_at < since
                controls = runtime.config.data["bilibili"]
                enabled = controls["owner_new_videos" if is_video else "owner_new_dynamics"]
                runtime.engine.ingest(event, baseline=old or not enabled or event.kind == "owner_share_only")
                if event.share_content:
                    repost = replace(event, event_id=event.event_id + ":repost", kind="owner_repost",
                                     content=event.share_content, share_content=event.content)
                    repost_enabled = controls.get("repost_owner_videos" if is_video else "repost_owner_dynamics", False)
                    runtime.engine.ingest(repost, baseline=old or not repost_enabled)
            if not initialized:
                runtime.store.set_setting(since_key, now - 2)
            runtime.store.set_setting(key, True)
        except PlatformError as exc:
            runtime.store.audit("owner_poll_error", str(exc), now)
            if exc.pause:
                runtime.engine.pause_platform(exc, "bilibili")
            LOG.warning("主人内容监测未完成：%s", exc)

    def poll_private(self, runtime, now: float | None = None):
        now = time.time() if now is None else now
        if runtime.engine.paused("bilibili") or not runtime.config.data["bilibili"].get("private_messages_enabled", True):
            return
        if self.monitor:
            if not self.monitor.allowed("private_inbox",now):
                return
            if not runtime.store.claim_poll_interval("private_inbox",now,runtime.config.data["bilibili"]["private_poll_seconds"]):
                return
        try:
            uid = self.adapter.account_id or self.adapter.account()["uid"]
            key = f"private_inbox:{uid}"
            checkpoint = runtime.store.get_setting(key)
            baseline = checkpoint is None
            since = checkpoint["since"] if checkpoint else now
            cursors = dict(checkpoint.get("cursors", {})) if checkpoint else {}
            batch = self.adapter.private_messages(now, since=since, cursors=cursors, baseline=baseline)
            self.record_inbox_warnings(runtime, "private", now)
            if runtime.engine.settings.platform == self.name:
                runtime.engine.settings = replace(runtime.engine.settings, account_id=self.adapter.account_id)
            # Persist events before advancing cursors. A crash between the two
            # re-reads duplicates, which enqueue_event safely rejects.
            for event in batch.events:
                runtime.engine.ingest(event, baseline=baseline)
            cursors.update(batch.cursors)
            runtime.store.set_setting(key, {"since": since, "cursors": cursors})
            if baseline:
                runtime.store.audit("private_initialized", "私信历史基线已建立，此后仅处理互关用户的新文字消息", now)
                LOG.info("私信历史基线已建立，仅处理此后的互关新消息")
            if batch.events or batch.denied_peers:
                runtime.store.audit("private_polled", f"读取新私信 {len(batch.events)} 条；非互关会话略过 {batch.denied_peers} 个", now)
                LOG.info("私信：新消息 %d 条；非互关会话略过 %d 个", len(batch.events), batch.denied_peers)
        except PlatformError as exc:
            runtime.engine.pause_platform(exc, "bilibili")
            runtime.store.audit("private_poll_error", str(exc), now)
            LOG.warning("私信读取未完成：%s", exc)


    def guard(self, config, action, now):
        if action.kind in {"browse_comment", "browse_reply", "recommendation"}:
            key = {"browse_comment": "comment_videos", "browse_reply": "reply_comments", "recommendation": "recommend_owner"}[action.kind]
            if not config.data["browsing"].get(key, True):
                return "对应主动评论、接话或推荐功能已关闭"
        if action.kind in {"like", "coin"}:
            key = "video_coin" if action.kind == "coin" else "video_like" if action.content and action.content.kind == "video" else "dynamic_like"
            if not config.data.get("engagement", {}).get(key, False):
                return "对应点赞或投币功能已关闭"
            if action.proactive and not config.data["browsing"]["enabled"]:
                return "自主浏览已关闭"
        if action.kind in {"private_reply", "proactive_private"} and not config.data["bilibili"].get("private_messages_enabled", True):
            return "私信回复功能已关闭"
        if action.kind == "owner_repost":
            key = "repost_owner_videos" if action.source_kind == "video" else "repost_owner_dynamics"
            if not config.data["bilibili"].get(key, False):
                return "主人转发功能已关闭"
        return ""

    def background(self, runtime, now):
        if self.artwork:
            self.artwork.run(now, agent_id=runtime.engine.settings.agent_id, platform=self.name,
                account_id=self.account_id, timezone=runtime.engine.settings.timezone,
                draft=runtime.engine.settings.draft, ttl_minutes=runtime.engine.settings.action_ttl_minutes,
                review_strategy=runtime.engine.settings.review_strategy if runtime.engine.settings.model_review else "none")

    def status(self, runtime):
        b = runtime.config.data["bilibili"]
        return {**super().status(runtime),
            "private_messages_enabled": b.get("private_messages_enabled", True),
            "private_reply_policy": "mutual_follow_only",
            "reply_poll_seconds": b.get("reply_poll_seconds", 15),
            "owner_poll_seconds": b["poll_seconds"],
            "video_analysis": "title_description_available_subtitles" if b.get("read_video_subtitles", True) else "title_description",
            "inbox_batch_limit": b.get("inbox_batch_limit", 90)}


def build_module(config, store):
    from murasamebot.adapters.platforms.bilibili_private_content import PrivateVideoProvider
    from murasamebot.adapters.platforms.bilibili import BilibiliAdapter
    from murasamebot.adapters.artwork.sources import PixivSource, XSource
    from murasamebot.adapters.request_monitor import RequestMonitor
    from murasamebot.application.request_policy import RequestSettings
    from murasamebot.application.artwork import ArtDiscovery, ArtSettings
    from murasamebot.bootstrap.owner_history import history_source, history_service
    monitor = RequestMonitor(store, RequestSettings(**config.data["requests"]))
    history_reader = history_source(config, store)
    history = history_service(config, store, history_reader)
    b = config.data["bilibili"]
    v = config.data.get("vision", {})
    platform = BilibiliAdapter((config.root / b["credentials"]).resolve(), expected_uid=str(b.get("robot_uid", "")),
                               pages=b["notification_pages"], max_age_hours=b["notification_max_age_hours"], proxy_url=config.network_proxy,
                               private_session_pages=b.get("private_session_pages", 3), private_message_pages=b.get("private_message_pages", 3),
                               private_max_age_hours=b.get("private_message_max_age_hours", 24),
                               read_video_subtitles=b.get("read_video_subtitles", True), inbox_batch_limit=b.get("inbox_batch_limit", 90),
                               proactive_recipient_id=(store.binding(config.data["owner_person_id"], "bilibili") or {}).get("external_id", ""), monitor=monitor,
                               video_frames_enabled=v.get("enabled", True) and v.get("video_frames_enabled", True),
                               video_frame_count=min(v.get("video_frame_count", 6), v.get("max_images", 9)),
                               video_frame_max_parts=v.get("video_frame_max_parts", 3),
                               frame_cache_minutes=v.get("cache_minutes", 1440), frame_retry_minutes=v.get("failure_retry_minutes", 10))
    a = config.data.get("artwork", {})
    sources = {}
    for source in a.get("sources", []):
        sources[source] = PixivSource(a.get("pixiv_session", ""), proxy_url=config.network_proxy) if source == "pixiv" else XSource(a.get("x_bearer_token", ""), proxy_url=config.network_proxy)
    artwork = ArtDiscovery(ArtSettings.from_config(a), sources, store, None, None)
    return BilibiliModule("bilibili", platform, CAPABILITIES, (history_reader,), history,
                          history_reader, artwork, monitor, b["reply_poll_seconds"],
                          private_content_provider=PrivateVideoProvider(platform))
