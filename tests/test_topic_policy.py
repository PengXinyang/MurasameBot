from dataclasses import replace
import json

import httpx
import pytest

from murasamebot.domain.models import Action, ContentRef, Document, CommentContext
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


def test_private_video_provider_reads_sensitive_search_and_subtitles_without_changing_public_defaults(tmp_path):
    from murasamebot.adapters.platforms.bilibili_private_content import PrivateVideoProvider
    p=BilibiliAdapter(credentials(tmp_path),video_frames_enabled=False)
    calls=[]
    def request(path,**kwargs):
        calls.append(path)
        if path.endswith('search/type'):
            return {"result":[{"aid":42,"title":"政治分析","description":"资料"}]},None
        return {"title":"政治分析","desc":"资料","bvid":"BVexample","pages":[{"cid":12}]},None
    p._request=request
    p._subtitle=lambda *args:"实际可得字幕"
    p._comments=lambda *args:()
    private=PrivateVideoProvider(p)
    try:
        assert p.search("政治分析",[],3)==[] and not calls
        refs=private.search("政治分析",[],3)
        assert len(refs)==1
        document=private.read(refs[0])
        assert document.evidence_level=="transcript" and "实际可得字幕" in document.text
        public=p.read(refs[0])
        assert public.topic_class=="sensitive" and public.evidence_level=="metadata"
        assert p.search("政治分析",[],3)==[]
    finally:p.close()


@pytest.mark.parametrize("grade",["sensitive","unknown"])
def test_final_policy_rejects_legacy_and_manually_approved_actions(tmp_path,grade):
    runtime=build(Config(write_config(tmp_path)))
    try:
        now=1000
        action=Action("topic",runtime.store.agent_id,"bilibili","robot",ContentRef("bilibili","video","42"),
                      "测试文字",now,now+100,approved_manually=True,topic_class=grade)
        assert "话题限制" in runtime.engine.action_policy(action,now)
    finally:runtime.close()


def test_final_policy_uses_target_chain_instead_of_other_speakers_cache(tmp_path):
    runtime=build(Config(write_config(tmp_path)))
    try:
        e,s=runtime.engine,runtime.store;now=1000
        event=replace(inbound(now),text='你好',reply_to='700',thread_root='600')
        e.ingest(event)
        casual=Document(event.content,'游戏舞蹈','轻松娱乐','text',comments=({'id':'700','text':'你好','author_id':'123'},),
            comment_context=CommentContext('700','600','complete'))
        sensitive=replace(casual,comments=({'id':'800','text':'政治讨论','author_id':'456'},),
            comment_context=CommentContext('800','600','complete'))
        s.cache_document(casual,now);s.cache_document(sensitive,now+1)
        a=Action('isolated',s.agent_id,'bilibili','robot',event.content,'吾辈在呢',now,now+100,
            event_id=event.event_id,reply_to='700',thread_root='600',topic_class='casual')
        assert e.action_policy(a,now+2)==''
        assert s.cached_reply_document(event.content,'700',now+2).comments==casual.comments
        assert '敏感' in e.action_policy(replace(a,reply_to='800'),now+2)
        # Isolation must not hide a genuine later update of the video itself.
        s.cache_document(replace(sensitive,title='政治分析'),now+3)
        assert '敏感' in e.action_policy(a,now+4)
    finally:runtime.close()


def test_unknown_target_cache_is_not_called_sensitive(tmp_path):
    runtime=build(Config(write_config(tmp_path)))
    try:
        now=1000;s=runtime.store;ref=ContentRef('bilibili','video','42')
        s.cache_document(Document(ref,'游戏','简介','text',comment_context=CommentContext('other','root','complete')),now)
        a=Action('missing-context',s.agent_id,'bilibili','robot',ref,'你好',now,now+100,reply_to='target',topic_class='casual')
        reason=runtime.engine.action_policy(a,now)
        assert '上下文' in reason and '本次未判定为敏感' in reason
    finally:runtime.close()
