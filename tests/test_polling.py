from dataclasses import replace
from types import SimpleNamespace

from murasamebot.bootstrap.runtime import Runtime
from murasamebot.domain.models import ContentRef, Event


def test_first_poll_baselines_and_late_old_owner_posts_do_not_flood(factory, now):
    engine, store, model, platform = factory()
    platform.account_id = "robot"
    old = Event("old-mention", "bilibili", "robot", "123", "thread", "mention", "旧消息", now - 10,
                content=platform.ref, reply_to="700")
    owner_old = Event("owner-old", "bilibili", "robot", "424242", "owner-thread", "owner_content", "旧内容", now - 100,
                      content=platform.ref)
    notifications = [old]
    feed = [owner_old]
    platform.notifications = lambda timestamp: list(notifications)
    platform.owner_content = lambda uid, timestamp: list(feed)
    config = SimpleNamespace(data={"owner_person_id": "owner", "bilibili": {"owner_new_videos": True, "owner_new_dynamics": True}})
    runtime = Runtime(config, store, platform, model, engine)
    runtime.poll(now)
    assert {row["state"] for row in store.inspect("events")} == {"baseline"}
    notifications.append(replace(old, event_id="new-mention", occurred_at=now + 10))
    feed += [replace(owner_old, event_id="newly-visible-old-post", occurred_at=now - 10000),
             replace(owner_old, event_id="owner-new", occurred_at=now + 10, content=ContentRef("bilibili", "video", "10002"))]
    runtime.poll(now + 10)
    states = {row["event_id"]: row["state"] for row in store.inspect("events")}
    assert states["old-mention"] == "baseline"
    assert states["new-mention"] == "pending"
    assert states["newly-visible-old-post"] == "baseline"
    assert states["owner-new"] == "pending"


def test_new_owner_binding_gets_its_own_baseline(factory, now):
    engine, store, model, platform = factory()
    platform.account_id = "robot"
    platform.notifications = lambda timestamp: []
    owner = ["424242"]
    platform.owner_content = lambda uid, timestamp: [Event("owner-" + uid, "bilibili", "robot", uid, "owner-thread", "owner_content", "内容", timestamp - 100, content=platform.ref)]
    config = SimpleNamespace(data={"owner_person_id": "owner", "bilibili": {"owner_new_videos": True, "owner_new_dynamics": True}})
    runtime = Runtime(config, store, platform, model, engine)
    runtime.poll(now)
    store.bind("owner", "bilibili", "222")
    runtime.poll(now + 10)
    assert {row["state"] for row in store.inspect("events")} == {"baseline"}
