from dataclasses import replace

import pytest

from murasamebot.application.recommendation_policy import popularity_allows
from murasamebot.bootstrap.config import Config
from murasamebot.bootstrap.platforms.bilibili import BilibiliModule
from murasamebot.domain.models import Action, ContentRef, Document, Event
from test_architecture_and_config import write_config


@pytest.mark.parametrize("views,comments,allowed", [(8000,21,False),(8001,20,False),(8001,21,True),
    (9000,19,False),(None,25,False),(9000,None,False),(True,25,False),(9000,"21",False)])
def test_strict_popularity_thresholds(views, comments, allowed):
    doc=Document(ContentRef("bilibili","video","42"),"title","text","metadata",view_count=views,comment_count=comments)
    assert popularity_allows(doc,8000,20) is allowed
    assert Document.from_dict(doc.public_context())==doc


@pytest.mark.parametrize("decision", ["comment","reply","recommend"])
def test_model_cannot_bypass_only_mode_or_add_secondary_reply(factory,now,decision):
    engine,store,model,platform=factory(recommendation_only=True,min_video_views=8000,min_video_comments=20)
    doc=replace(platform.read(platform.ref),view_count=8001,comment_count=21)
    model.evaluation.update(decision=decision,reply_to="700",comment_reply={"reply_to":"700","text":"接话"})
    result=engine._evaluate(doc,now)
    rows=store.inspect("actions")
    assert result is (decision=="recommend")
    assert all(row["kind"]=="recommendation" for row in rows)
    request=next(p for op,p in model.calls if op=="evaluate_video")
    assert not request["comment_videos"] and not request["reply_comments"]


def test_low_counts_block_even_model_recommendation(factory,now):
    engine,store,model,platform=factory(recommendation_only=True,min_video_views=8000,min_video_comments=20)
    assert not engine._evaluate(replace(platform.read(platform.ref),view_count=8000,comment_count=30),now)
    assert not store.inspect("actions")
    assert not any(op=="evaluate_video" for op,p in model.calls)


@pytest.mark.parametrize("route", [{"mode":"private"},{"mode":"public_mention"},{}])
def test_explicit_tasks_cannot_bypass_popularity(factory,now,route):
    engine,store,model,platform=factory(min_video_views=8000,min_video_comments=20)
    doc=replace(platform.read(platform.ref),view_count=34,comment_count=21)
    assert not engine._evaluate(doc,now,task={"route":route})
    assert not model.calls and not store.inspect("actions")


def test_explicit_video_mention_is_exempt(factory,now):
    engine,store,model,platform=factory(recommendation_only=True,min_video_views=8000,min_video_comments=20)
    platform.documents[platform.ref.key]=replace(platform.read(platform.ref),view_count=34,comment_count=0)
    engine.ingest(Event("new-comment","bilibili","robot","123","thread","mention","测试一下",now,content=platform.ref))
    assert engine.process_one_event(now)
    assert store.inspect("actions")[0]["kind"]=="reply"


def test_reply_without_mention_is_not_exempt(factory,now):
    engine,store,model,platform=factory(recommendation_only=True,min_video_views=8000,min_video_comments=20)
    platform.documents[platform.ref.key]=replace(platform.read(platform.ref),view_count=34,comment_count=30)
    engine.ingest(Event("new-comment","bilibili","robot","123","thread","reply","测试一下",now,content=platform.ref))
    engine.process_one_event(now)
    assert not store.inspect("actions")


def test_owner_new_content_is_not_proactively_commented(factory,now):
    engine,store,model,platform=factory(recommendation_only=True)
    engine.ingest(Event("owner-new","bilibili","robot","424242","thread","owner_content","新视频",now,content=platform.ref))
    engine.process_one_event(now)
    assert not any(row["kind"]=="owner_comment" for row in store.inspect("actions"))


def test_config_defaults_save_and_live_policy(tmp_path,now):
    config=Config(write_config(tmp_path))
    assert config.behaviour().recommendation_only
    assert config.behaviour().min_video_views==8000 and config.behaviour().min_video_comments==20
    config.save_local({"browsing":{"recommendation_only":False,"min_video_views":100,"min_video_comments":3}})
    current=Config(config.path)
    assert not current.behaviour().recommendation_only and current.behaviour().min_video_views==100
    current.save_local({"browsing":{"recommendation_only":True}})
    action=Action("a","agent","bilibili","bot",ContentRef("bilibili","dynamic","42"),"text",now,now+100,kind="browse_comment")
    assert BilibiliModule("bilibili",None,frozenset()).guard(Config(config.path),action,now)
    assert not BilibiliModule("bilibili",None,frozenset()).guard(Config(config.path),replace(action,kind="reply"),now)


@pytest.mark.parametrize("key,value", [("min_video_views",True),("min_video_comments",-1),("min_video_views",1.5),("recommendation_only","true")])
def test_invalid_config_rejected(tmp_path,key,value):
    config=Config(write_config(tmp_path))
    with pytest.raises(ValueError):
        config.save_local({"browsing":{key:value}})


def test_legacy_cache_missing_counts_is_refreshed(factory,now):
    engine,store,model,platform=factory(min_video_views=8000,min_video_comments=20)
    store.cache_document(replace(platform.read(platform.ref),view_count=None,comment_count=None),now)
    doc=engine.document(platform.ref,now+1)
    assert doc.view_count==9000 and doc.comment_count==30


def test_bilibili_read_uses_platform_counts_not_model_estimates(tmp_path):
    import httpx
    from murasamebot.adapters.platforms.bilibili import BilibiliAdapter
    from test_bilibili_contracts import credentials
    adapter=BilibiliAdapter(credentials(tmp_path),read_video_subtitles=False,
        client=httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={"code":0,"data":{}}))))
    adapter._request=lambda path,**kwargs: ({"title":"标题","desc":"简介","owner":{"mid":123},"stat":{"view":9001,"reply":25}} if path.endswith('/view') else {"replies":[]},None)
    try:
        doc=adapter.read(ContentRef('bilibili','video','42'))
        assert doc.view_count==9001 and doc.comment_count==25
    finally:
        adapter.close()


def test_runtime_rechecks_threshold_changes_and_mention_source(tmp_path,now):
    from conftest import TestPlatform
    from murasamebot.bootstrap.runtime import build
    from murasamebot.bootstrap.platforms.bilibili import CAPABILITIES
    config=Config(write_config(tmp_path))
    config.save_local({"mode":"auto"})
    platform=TestPlatform()
    platform.account_id,platform.configured="robot",True
    platform.close=lambda:None
    runtime=build(Config(config.path),platform_factories={"bilibili":lambda c,s:BilibiliModule("bilibili",platform,CAPABILITIES)})
    try:
        doc=replace(platform.read(platform.ref),view_count=9000,comment_count=30,topic_class="casual")
        runtime.store.cache_document(doc,now)
        action=Action("a",runtime.store.agent_id,"bilibili","robot",platform.ref,"推荐",now,now+300,kind="recommendation",topic_class="casual")
        assert not runtime.engine.action_policy(action,now)
        config.save_local({"browsing":{"min_video_views":10000}})
        assert "门槛" in runtime.engine.action_policy(action,now)
        assert "门槛" in runtime.engine.action_policy(replace(action,task_id="explicit-task"),now)
        event=Event("mention-source","bilibili","robot","123","thread","mention","聊聊",now,content=platform.ref)
        runtime.engine.ingest(event)
        assert "门槛" not in runtime.engine.action_policy(replace(action,kind="reply",event_id=event.event_id),now)
        assert "门槛" in runtime.engine.action_policy(replace(action,kind="reply",event_id="missing"),now)
    finally:
        runtime.close()
