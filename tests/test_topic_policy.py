from dataclasses import replace
import json

import httpx
import pytest

from murasamebot.domain.models import Action, ContentRef, Document
from murasamebot.adapters.platforms.bilibili import BilibiliAdapter
from murasamebot.bootstrap.config import Config
from murasamebot.bootstrap.runtime import build
from test_architecture_and_config import write_config
from test_bilibili_contracts import credentials
from test_model_review import inbound


@pytest.mark.parametrize("owner",[False,True])
def test_political_source_never_generates_or_reacts_even_for_owner(factory,now,owner):
    e,s,m,p=factory(video_like=True,video_coin=True)
    event=inbound(now)
    if owner:event=replace(event,actor_id="424242")
    p.documents[event.content.key]=Document(event.content,"时政与大选分析","资料","text")
    e.ingest(event);e.process_one_event(now)
    assert not m.calls and not s.inspect("actions")


@pytest.mark.parametrize("owner",[False,True])
def test_serious_topic_exception_uses_bound_actor(factory,now,owner):
    e,s,m,p=factory()
    event=replace(inbound(now),actor_id="424242" if owner else "123",actor_name="主人")
    p.documents[event.content.key]=Document(event.content,"悼念逝者","庄重讨论","text")
    m.reply.update(topic_class="serious")
    e.ingest(event);e.process_one_event(now)
    assert bool(s.inspect("actions"))==owner


@pytest.mark.parametrize("grade",["serious","sensitive","unknown",None,False])
def test_semantic_grade_blocks_even_with_publish_approval(factory,now,grade):
    e,s,m,p=factory(video_like=True,video_coin=True)
    m.reply.update(topic_class=grade,can_send=True,review_reason="通过",interested=True)
    e.ingest(inbound(now));e.process_one_event(now)
    assert not s.inspect("actions")


def test_browse_semantic_sensitive_cannot_like_coin_recommend_or_reply(factory,now):
    e,s,m,p=factory(video_like=True,video_coin=True)
    m.evaluation.update(topic_class="sensitive",interested=True,
                        comment_reply={"reply_to":"700","text":"吾辈也来讨论。","topic_class":"casual"})
    e.browse(now)
    assert not s.inspect("actions")


def test_serious_extra_reply_rejected_independently(factory,now):
    e,s,m,p=factory()
    m.evaluation.update(decision="skip",topic_class="casual",comment_reply={"reply_to":"700","text":"吾辈来回答。","topic_class":"serious"})
    e.browse(now)
    assert not s.inspect("actions")


def test_political_search_and_metadata_do_not_read_comments_or_subtitles(tmp_path):
    calls=[]
    def handle(request):
        calls.append(request.url.path)
        if request.url.path.endswith("newlist"):
            return httpx.Response(200,json={"code":0,"data":{"archives":[{"aid":42,"title":"政治分析"},{"aid":43,"title":"游戏介绍"}]}})
        assert request.url.path.endswith("view")
        return httpx.Response(200,json={"code":0,"data":{"title":"政治分析","desc":"资料","pages":[{"cid":12}]}})
    p=BilibiliAdapter(credentials(tmp_path),client=httpx.Client(transport=httpx.MockTransport(handle)))
    assert p.search("政治分析",[],3)==[] and not calls
    assert [r.external_id for r in p.discover(["4"],3)]==["43"]
    doc=p.read(ContentRef("bilibili","video","42"),reply_to="700",root="600")
    assert doc.topic_class=="sensitive" and not doc.comments
    assert calls==["/x/web-interface/newlist","/x/web-interface/view"]


@pytest.mark.parametrize("grade",["sensitive","unknown"])
def test_final_policy_rejects_legacy_and_manually_approved_actions(tmp_path,grade):
    runtime=build(Config(write_config(tmp_path)))
    try:
        now=1000
        action=Action("topic",runtime.store.agent_id,"bilibili","robot",ContentRef("bilibili","video","42"),
                      "测试文字",now,now+100,approved_manually=True,topic_class=grade)
        assert "话题限制" in runtime.engine.action_policy(action,now)
    finally:runtime.close()
