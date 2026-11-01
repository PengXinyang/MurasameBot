from dataclasses import replace

import pytest

from murasamebot.application.recommendation_policy import popularity_allows, reactive_video_reply
from murasamebot.domain.models import Action, Event


@pytest.mark.parametrize("author,allowed", [("424242", True), ("robot", True), ("other", False), ("", False)])
def test_author_exemption_is_not_the_commenter_identity(factory, author, allowed):
    engine, _, _, platform = factory(min_video_views=8000, min_video_comments=20)
    doc = replace(platform.read(platform.ref), author_id=author, view_count=None, comment_count=None)
    assert engine.popularity_allowed(doc) is allowed


@pytest.mark.parametrize("kind", ["mention", "reply"])
def test_low_volume_inbound_conversation_can_continue(factory, now, kind):
    engine, store, _, platform = factory(reactive_video_comments_only=True, min_video_views=8000, min_video_comments=20)
    platform.documents[platform.ref.key] = replace(platform.read(platform.ref), view_count=1, comment_count=0)
    engine.ingest(Event("in", "bilibili", "robot", "123", "thread", kind, "聊聊", now,
                       content=platform.ref, reply_to="700"))
    engine.process_one_event(now)
    assert store.inspect("actions")[0]["kind"] == "reply"


@pytest.mark.parametrize("decision", ["comment", "reply", "recommend"])
def test_no_autonomous_video_comment_even_if_legacy_switches_enabled(factory, now, decision):
    engine, store, model, platform = factory(reactive_video_comments_only=True, recommendation_only=False)
    model.evaluation.update(decision=decision, reply_to="700")
    assert not engine._evaluate(platform.read(platform.ref), now)
    assert not store.inspect("actions")


def test_source_cannot_authorize_another_target_or_missing_notification(factory, now):
    _, _, _, platform = factory()
    event = Event("in", "bilibili", "robot", "123", "thread", "reply", "聊聊", now,
                  content=platform.ref, reply_to="700")
    action = Action("a", "agent", "bilibili", "robot", platform.ref, "回复", now, now+60, reply_to="700")
    assert reactive_video_reply(action,event)
    assert not reactive_video_reply(replace(action,reply_to="701"),event)
    assert not reactive_video_reply(replace(action,account_id="other"),event)
    assert not reactive_video_reply(action,None)


def test_runtime_blocks_old_proactive_actions_but_allows_same_target_reply(tmp_path,now):
    from conftest import TestPlatform
    from murasamebot.bootstrap.config import Config
    from murasamebot.bootstrap.platforms.bilibili import BilibiliModule, CAPABILITIES
    from murasamebot.bootstrap.runtime import build
    from test_architecture_and_config import write_config
    config=Config(write_config(tmp_path))
    platform=TestPlatform()
    platform.account_id,platform.configured="robot",True
    platform.close=lambda:None
    runtime=build(config,platform_factories={"bilibili":lambda c,s:BilibiliModule("bilibili",platform,CAPABILITIES)})
    try:
        runtime.store.cache_document(replace(platform.read(platform.ref),view_count=1,comment_count=0,topic_class="casual"),now)
        action=Action("a",runtime.store.agent_id,"bilibili","robot",platform.ref,"回复",now,now+60,kind="recommendation",topic_class="casual")
        assert "禁止主动评论" in runtime.engine.action_policy(action,now)
        event=Event("in","bilibili","robot","123","thread","reply","聊聊",now,content=platform.ref,reply_to="700")
        runtime.engine.ingest(event)
        assert not runtime.engine.action_policy(replace(action,kind="reply",reply_to="700",event_id="in"),now)
        assert runtime.engine.action_policy(replace(action,kind="reply",reply_to="701",event_id="in"),now)
    finally:
        runtime.close()
