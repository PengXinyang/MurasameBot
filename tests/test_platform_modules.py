"""Platform composition contracts; fake QQ transport, never real social writes."""
import ast
import builtins
from dataclasses import replace

import pytest
import yaml

from murasamebot.bootstrap.config import Config
from murasamebot.bootstrap.platforms import PlatformModule, build_modules
from murasamebot.bootstrap.runtime import build, prepare_store, readiness
from murasamebot.domain.models import Action, Event, PlatformError, Receipt
from test_architecture_and_config import ROOT, write_config


class FakeQQ:
    account_id = "qq-bot"
    configured = True

    def __init__(self):
        self.sent = []
        self.closed = 0

    def allows_private_reply(self, account_id, peer_id):
        return account_id == self.account_id and peer_id != account_id

    def send(self, action, mentions, *, before_write=None):
        assert action.platform == "qq" and action.account_id == self.account_id
        if before_write and not before_write():
            return Receipt("failed", detail="guarded")
        self.sent.append(action)
        return Receipt("accepted", "qq-message")

    def close(self):
        self.closed += 1


def qq_config(tmp_path, **options):
    path = write_config(tmp_path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["platforms"] = {"bilibili": {"enabled": False}, "qq": {"enabled": True}}
    data.pop("bilibili")  # No Bilibili credentials, polling or private-chat config.
    data["browsing"]["enabled"] = True
    data["owner_history"]["enabled"] = True
    data["artwork"]["enabled"] = True
    data["autonomy"]["dynamic_enabled"] = True
    data.update(options)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return Config(path)


def qq_runtime(config):
    adapter = FakeQQ()
    return build(config, platform_factories={"qq": lambda c, s: PlatformModule(
        "qq", adapter, frozenset({"private_reply", "reply"}))})


def action(now, platform="qq", account="qq-bot", *, key="a", **kwargs):
    return Action(key, "murasame", platform, account, None, "吾辈记住了。", now, now + 3600,
                  kind="reply", review_state="passed", review_method="inline", **kwargs)


def test_qq_only_build_and_tick_never_import_bilibili_or_art_sources(tmp_path, monkeypatch):
    config = qq_config(tmp_path)
    original = builtins.__import__
    imports = []

    def guarded(name, *args, **kwargs):
        imports.append(name)
        assert not name.startswith("murasamebot.adapters.platforms.bilibili")
        assert name != "murasamebot.adapters.artwork.sources"
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    runtime = qq_runtime(config)
    try:
        assert runtime.platform.account_id == "qq-bot"
        assert runtime.owner_history is None and runtime.artwork is None and runtime.network_monitor is None
        assert runtime.engine.providers == {}
        assert not runtime.engine.settings.browsing_enabled
        assert not runtime.engine.settings.autonomy.dynamic_enabled
        runtime.tick()
        report = readiness(runtime)
        assert report["primary_platform"] == "qq"
        assert list(report["platforms"]) == ["qq"]
        assert not report["browsing_enabled"] and not report["artwork_enabled"]
        with pytest.raises(ValueError, match="未启用"):
            runtime.require_platform("bilibili")
    finally:
        runtime.close()
    assert runtime.platform.closed == 1
    assert not list(tmp_path.rglob("bilibili*.json"))


def test_fake_qq_conversation_round_trip_uses_one_shared_engine(tmp_path, now):
    runtime = qq_runtime(qq_config(tmp_path))
    class Model:
        def decide(self, operation, payload):
            assert operation == "private_chat"
            return {"decision": "reply", "text": "吾辈听见了。", "can_send": True, "review_reason": "正常聊天"}
    runtime.engine.model = Model()
    event = Event("42", "qq", "qq-bot", "peer", "room", "private_message", "丛雨在吗？", now, "private")
    try:
        assert runtime.ingest(event)
        assert not runtime.ingest(event)
        assert runtime.engine.process_one_event(now)
        assert runtime.engine.dispatch_one(now)
        assert len(runtime.platform.sent) == 1
        sent = runtime.platform.sent[0]
        assert sent.platform == "qq" and sent.recipient_id == "peer"
        assert not runtime.engine.dispatch_one(now + 20)
        assert runtime.store.inspect("events")[0]["state"] == "done"
    finally:
        runtime.close()


@pytest.mark.parametrize("change", [{"account_id": "wrong"}, {"platform": "bilibili"}, {"event_id": ""}, {"conversation_id": ""}])
def test_push_input_rejects_wrong_route_or_missing_identity(tmp_path, now, change):
    runtime = qq_runtime(qq_config(tmp_path))
    try:
        event = Event("42", "qq", "qq-bot", "peer", "room", "private_message", "你好", now, "private")
        with pytest.raises(ValueError):
            runtime.ingest(replace(event, **change))
        assert runtime.store.inspect("events") == []
    finally:
        runtime.close()


def test_event_namespace_separates_platform_and_robot_account(now):
    raw = Event("42", "qq", "qq-bot", "peer", "room", "private_message", "你好", now, "private")
    one = PlatformModule("qq", FakeQQ()).normalize_event(raw)
    two = PlatformModule("other", FakeQQ()).normalize_event(replace(raw, platform="other"))
    three = PlatformModule("qq", FakeQQ()).normalize_event(replace(raw, account_id="another-bot"))
    assert len({one.event_id, two.event_id, three.event_id}) == 3
    assert len({one.conversation_id, two.conversation_id, three.conversation_id}) == 3


def test_disabled_and_wrong_account_actions_are_not_claimed_or_failed(tmp_path, now):
    runtime = qq_runtime(qq_config(tmp_path))
    try:
        for a in (action(now, "bilibili", "bili-bot", key="bili"),
                  action(now, account="old-qq-bot", key="old"), action(now, key="qq")):
            runtime.store.enqueue_action(a, draft=False, now=now)
        assert runtime.engine.dispatch_one(now)
        assert [a.action_id for a in runtime.platform.sent] == ["qq"]
        states = {r["action_id"]: r["state"] for r in runtime.store.inspect("actions")}
        assert states == {"bili": "queued", "old": "queued", "qq": "accepted"}
    finally:
        runtime.close()


def test_disabled_review_and_event_remain_pending_after_restart(tmp_path, now):
    config = qq_config(tmp_path)
    runtime = qq_runtime(config)
    try:
        runtime.store.enqueue_event(Event("old-bili", "bilibili", "bili-bot", "peer", "r", "reply", "旧消息", now))
        runtime.store.enqueue_action(action(now, "bilibili", "bili-bot"), draft=False, now=now)
        assert not runtime.engine.process_one_event(now)
        assert not runtime.engine.review_one(now)
        status = runtime.store.queue_status(now=now, routes=runtime.engine.routing())
        assert status["ready_events"] == status["ready_actions"] == status["review_ready_actions"] == 0
        assert status["inactive_events"] == 1
    finally:
        runtime.close()
    restarted = qq_runtime(Config(config.path))
    try:
        assert not restarted.engine.dispatch_one(now)
        assert restarted.store.inspect("events")[0]["state"] == "pending"
        assert restarted.store.inspect("actions")[0]["state"] == "queued"
    finally:
        restarted.close()


def test_live_disable_blocks_claim_and_manual_send_without_destroying_queue(tmp_path, now):
    runtime = qq_runtime(qq_config(tmp_path))
    try:
        a = action(now)
        runtime.store.enqueue_action(a, draft=False, now=now)
        runtime.config.save_local({"platforms": {"qq": {"enabled": False}}})
        assert not runtime.engine.dispatch_one(now)
        assert runtime.store.inspect("actions")[0]["state"] == "queued"
        assert "未启用" in runtime.engine.action_policy(a, now)
        runtime.store.enqueue_action(replace(a, action_id="draft", dedup_key="draft"), draft=True, now=now)
        with pytest.raises(ValueError):
            runtime.engine.queue_manual_send("draft", now)
    finally:
        runtime.close()


def test_manual_send_requires_known_matching_robot_account(tmp_path, now):
    runtime = qq_runtime(qq_config(tmp_path))
    try:
        runtime.store.enqueue_action(action(now), draft=True, now=now)
        runtime.platform.account_id = ""
        with pytest.raises(ValueError, match="账号或平台已变化"):
            runtime.engine.queue_manual_send("a", now)
        assert runtime.store.inspect("actions")[0]["state"] == "draft"
    finally:
        runtime.close()


def test_unsupported_capability_is_rejected_by_final_policy(tmp_path, now):
    runtime = qq_runtime(qq_config(tmp_path))
    try:
        assert "不支持" in runtime.engine.action_policy(replace(action(now), kind="coin"), now)
        assert "账号已变化" in runtime.engine.action_policy(action(now, account="other"), now)
    finally:
        runtime.close()


def test_registered_qq_is_not_a_bilibili_fallback(tmp_path):
    config = qq_config(tmp_path)
    runtime = build(config)
    try:
        from murasamebot.adapters.platforms.qq import QQAdapter
        assert isinstance(runtime.platform, QQAdapter)
        assert not runtime.platform.configured
    finally:
        runtime.close()


@pytest.mark.parametrize("platforms", [[], {"QQ": {"enabled": True}}, {"qq": True}, {"qq": {"enabled": "false"}}, {"qq": {"enabled": True, "token": "secret"}}])
def test_platform_configuration_is_strict(tmp_path, platforms):
    with pytest.raises(ValueError, match="平台|platforms"):
        qq_config(tmp_path, platforms=platforms)


def test_legacy_config_and_bilibili_import_interface_are_preserved(tmp_path):
    path = write_config(tmp_path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data.pop("platforms")
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    config = Config(path)
    assert config.enabled_platforms == ("bilibili",)
    runtime = build(config)
    try:
        from murasamebot.adapters.platforms.bilibili import BilibiliAdapter
        assert isinstance(runtime.platform, BilibiliAdapter)
        assert runtime.require_platform("bilibili") is runtime.platform
        assert runtime.modules["bilibili"].poll_seconds == config.data["bilibili"]["reply_poll_seconds"]
        assert readiness(runtime)["private_reply_policy"] == "mutual_follow_only"
    finally:
        runtime.close()


def test_owner_new_content_comment_survives_platform_capability_gate(tmp_path, now):
    from conftest import TestModel, TestPlatform
    from murasamebot.domain.models import Document
    from murasamebot.bootstrap.platforms.bilibili import BilibiliModule, CAPABILITIES
    config = Config(write_config(tmp_path))
    config.save_local({"mode": "auto", "browsing": {"recommendation_only": False}})
    adapter = TestPlatform()
    adapter.account_id, adapter.configured = "robot", True
    adapter.close = lambda: None
    adapter.emoji_options = lambda *args: ()
    adapter.documents[adapter.ref.key] = Document(adapter.ref, "摄影入门", "曝光和构图的练习方法。", "transcript", "424242", "188",view_count=9000,comment_count=30)
    runtime = build(Config(config.path), platform_factories={"bilibili": lambda c, s:
        BilibiliModule("bilibili", adapter, CAPABILITIES)})
    runtime.engine.model = TestModel()
    try:
        event = Event("owner-new", "bilibili", "robot", "424242", "owner-thread", "owner_content", "新视频", now, content=adapter.ref)
        assert runtime.ingest(event)
        assert runtime.engine.process_one_event(now)
        assert runtime.engine.dispatch_one(now)
        assert len(adapter.sent) == 1 and adapter.sent[0][0].kind == "owner_comment"
        assert runtime.store.inspect("actions")[0]["state"] == "accepted"
    finally:
        runtime.close()


def test_bilibili_settings_change_only_cancels_bilibili_plans(tmp_path, now):
    config = Config(write_config(tmp_path))
    store = prepare_store(config)
    for p in ("bilibili", "qq"):
        assert store.enqueue_action(replace(action(now, p, key=p), kind="autonomous_post",
            autonomy_version=config.behaviour().autonomy.version(), not_before=now), draft=True, now=now)
    config.save_local({"autonomy": {"dynamic_enabled": True}})
    prepare_store(Config(config.path))
    assert {r["action_id"]: r["state"] for r in store.inspect("actions")} == {"bilibili": "cancelled", "qq": "draft"}


def test_factory_failure_closes_already_created_platforms(tmp_path):
    config = qq_config(tmp_path, platforms={"bilibili": {"enabled": False}, "qq": {"enabled": True}, "other": {"enabled": True}})
    adapter = FakeQQ()
    def fail(config, store):
        raise ValueError("failed construction")
    with pytest.raises(ValueError, match="failed construction"):
        build_modules(config, prepare_store(config), {"qq": lambda c, s: PlatformModule("qq", adapter), "other": fail})
    assert adapter.closed == 1


def test_bilibili_error_does_not_stop_other_module_polling(tmp_path):
    runtime = qq_runtime(qq_config(tmp_path))
    class Broken(PlatformModule):
        def poll(self, runtime, now):
            raise PlatformError("offline", code="offline", pause=True)
    class Healthy(PlatformModule):
        def poll(self, runtime, now):
            runtime.store.set_setting("qq_polled", True)
    runtime.modules = {"bilibili": Broken("bilibili", FakeQQ()), "qq": Healthy("qq", runtime.platform)}
    try:
        runtime.poll(1000)
        assert runtime.store.get_setting("platform_paused:bilibili")
        assert not runtime.engine.paused("qq")
        assert runtime.store.get_setting("qq_polled")
    finally:
        runtime.close()


def test_shared_bootstrap_has_no_eager_platform_adapter_imports():
    for name in ("runtime.py", "cli.py", "workers.py", "owner_history.py"):
        tree = ast.parse((ROOT / "src/murasamebot/bootstrap" / name).read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("murasamebot.adapters.platforms")


def test_one_sender_routes_multiple_platforms_and_keeps_global_gap(tmp_path, now):
    config = qq_config(tmp_path, platforms={"bilibili": {"enabled": False}, "qq": {"enabled": True}, "other": {"enabled": True}})
    qq, other = FakeQQ(), FakeQQ()
    other.account_id = "other-bot"
    def send_other(a, mentions, *, before_write=None):
        assert a.platform == "other" and a.account_id == "other-bot"
        assert before_write is None or before_write()
        other.sent.append(a)
        return Receipt("accepted", "other-message")
    other.send = send_other
    runtime = build(config, platform_factories={name: (lambda c, s, name=name, adapter=adapter:
        PlatformModule(name, adapter, frozenset({"reply"}))) for name, adapter in (("qq", qq), ("other", other))})
    try:
        runtime.store.enqueue_action(action(now, key="qq"), draft=False, now=now)
        runtime.store.enqueue_action(action(now, "other", "other-bot", key="other"), draft=True, now=now)
        # Manual sending uses the action's registered route, not the primary platform.
        runtime.engine.queue_manual_send("other", now)
        assert runtime.engine.dispatch_one(now)
        assert not runtime.engine.dispatch_one(now + 1)
        assert runtime.engine.dispatch_one(now + 11)
        assert [a.action_id for a in qq.sent] == ["qq"]
        assert [a.action_id for a in other.sent] == ["other"]
    finally:
        runtime.close()


def test_disabled_linked_private_event_does_not_block_active_platform(tmp_path, now):
    runtime = qq_runtime(qq_config(tmp_path))
    try:
        runtime.store.bind("owner", "bilibili", "bili-peer")
        runtime.store.bind("owner", "qq", "qq-peer")
        for key, platform, account, peer in (("old", "bilibili", "bili-bot", "bili-peer"), ("new", "qq", "qq-bot", "qq-peer")):
            runtime.store.enqueue_event(Event(key, platform, account, peer, "room", "private_message", "消息", now, "private"))
        assert runtime.store.claim_event(now, routes=runtime.engine.routing()).event_id == "new"
        assert {r["event_id"]: r["state"] for r in runtime.store.inspect("events")} == {"old": "pending", "new": "processing"}
    finally:
        runtime.close()


def test_all_platforms_disabled_is_valid_offline_runtime(tmp_path):
    config = qq_config(tmp_path, platforms={"bilibili": {"enabled": False}})
    runtime = build(config)
    try:
        assert runtime.modules == {} and runtime.platform is None
        assert runtime.engine.routing() == ()
        runtime.tick()
        assert not readiness(runtime)["login_configured"]
    finally:
        runtime.close()


def test_shared_build_failure_closes_created_platform(tmp_path):
    config = qq_config(tmp_path, character_source="does-not-exist.yml")
    adapter = FakeQQ()
    with pytest.raises(OSError):
        build(config, platform_factories={"qq": lambda c, s: PlatformModule("qq", adapter)})
    assert adapter.closed == 1


def test_bilibili_private_switch_does_not_disable_qq_reply(tmp_path, now):
    path = write_config(tmp_path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["platforms"]["qq"] = {"enabled": True}
    data["bilibili"]["private_messages_enabled"] = False
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    from murasamebot.bootstrap.platforms.bilibili import build_module
    qq = FakeQQ()
    runtime = build(Config(path), platform_factories={"bilibili": build_module,
        "qq": lambda c, s: PlatformModule("qq", qq, frozenset({"reply", "private_reply"}))})
    try:
        assert "私信回复功能已关闭" in runtime.engine.action_policy(replace(action(now, "bilibili", key="b"), kind="private_reply"), now)
        assert runtime.engine.action_policy(replace(action(now), kind="private_reply"), now) == ""
        assert runtime.engine.dispatch_one(now) is False
    finally:
        runtime.close()


@pytest.mark.parametrize("url", ["/api/social", "/api/memory/journals", "/api/owner-history"])
def test_shared_dashboard_pages_do_not_require_bilibili(tmp_path, monkeypatch, url):
    from test_web_disconnects import request
    config = qq_config(tmp_path)
    original = builtins.__import__
    def guarded(name, *args, **kwargs):
        assert not name.startswith("murasamebot.adapters.platforms.bilibili")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded)
    handler = request(config.path, url=url)
    handler.do_GET()
    assert b"200 OK" in handler.wfile.getvalue()
