from dataclasses import replace
import json

import httpx
import pytest

from murasamebot.adapters.platforms.bilibili import BilibiliAdapter
from murasamebot.application.recommendation_policy import VIDEO_INTEREST_KEYWORDS, interest_exempt
from murasamebot.bootstrap.config import Config
from murasamebot.bootstrap.platforms.bilibili import BilibiliModule, CAPABILITIES
from murasamebot.bootstrap.runtime import build
from murasamebot.domain.models import Action, ContentRef, Document, Event, PlatformError
from conftest import owner_message, TestPlatform
from test_architecture_and_config import write_config
from test_bilibili_contracts import credentials


def selection_engine(factory, **settings):
    e,s,m,p=factory(video_selection_mode="read_then_decide",min_video_views=8000,min_video_comments=20,
        recommendation_only=True,reactive_video_comments_only=True,**settings)
    base=p.read(p.ref)
    calls=[]
    metadata=replace(base,evidence_level="metadata",text="公开简介",comments=(),images=(),visual=None,frame_context=None)
    p.preview=lambda ref:(calls.append("preview") or metadata)
    p.read=lambda ref,**kw:(calls.append("read") or base)
    class Vision:
        def needs_refresh(self,*args):return False
        def enrich(self,document,*args,**kw):calls.append("vision");return document
    e.vision=Vision()
    m.evaluation.update(decision="skip",interested=False)
    return e,s,m,p,calls,metadata


@pytest.mark.parametrize("views,comments", [(8000,21),(8001,20),(None,99),(99999,None),(True,99),(99999,"30"),(0,0)])
def test_low_or_unknown_counts_stop_before_content_vision_model_and_do_not_mark_watched(factory,now,views,comments):
    e,s,m,p,calls,meta=selection_engine(factory)
    p.preview=lambda ref:(calls.append("preview") or replace(meta,view_count=views,comment_count=comments))
    assert e.browse(now)==0
    assert calls==["preview"] and not m.calls and not s.inspect("actions")
    assert not s.has_viewed(p.ref,now) and not s.cached_document(p.ref,now)


@pytest.mark.parametrize("title,tags", [("千恋万花",()),("GALGAME实况",()),("柚子社新作",()),
    ("ゆずソフト",()),("YuzuSoft 新作",()),("Key社作品",()),("作品介绍",("SAGA PLANETS",)),
    ("普通标题",("galgame",)),("TYPE-MOON作品",()),("Purple software",())])
def test_interest_title_or_tags_allow_read_even_without_statistics(factory,now,title,tags):
    e,s,m,p,calls,meta=selection_engine(factory)
    p.preview=lambda ref:(calls.append("preview") or replace(meta,title=title,tags=tags,view_count=None,comment_count=None))
    assert e.browse(now)==1
    assert calls==["preview","read","vision"]
    assert any(op=="evaluate_video" for op,_ in m.calls)
    assert not s.inspect("actions")  # AI may still skip.
    assert Document.from_dict(s.cached_document(p.ref,now).public_context()).tags==tags


@pytest.mark.parametrize("author",["424242","robot"])
def test_verified_owner_and_current_bot_authors_bypass_both_counts(factory,now,author):
    e,s,m,p,calls,meta=selection_engine(factory)
    p.preview=lambda ref:(calls.append("preview") or replace(meta,author_id=author,view_count=0,comment_count=0))
    assert e.browse(now)==1 and "vision" in calls


def test_description_keywords_and_partial_studio_words_are_not_title_tag_exceptions(factory,now):
    e,s,m,p,calls,meta=selection_engine(factory)
    p.preview=lambda ref:(calls.append("preview") or replace(meta,title="monkey keyboard",text="galgame 柚子社",view_count=0,comment_count=0))
    assert e.browse(now)==0 and calls==["preview"]
    assert not interest_exempt(replace(meta,title="monkey keyboard"),VIDEO_INTEREST_KEYWORDS)


@pytest.mark.parametrize("decision,kind",[("skip",None),("comment","browse_comment"),("recommend","recommendation")])
def test_selected_video_can_be_skipped_commented_or_recommended_despite_legacy_flags(factory,now,decision,kind):
    e,s,m,p,calls,meta=selection_engine(factory)
    m.evaluation.update(decision=decision)
    assert e.browse(now)==1
    rows=s.inspect("actions")
    assert [r["kind"] for r in rows]==([kind] if kind else [])
    payload=next(payload for op,payload in m.calls if op=="evaluate_video")
    assert payload["comment_videos"] and payload["recommend_owner"]
    assert not rows or json.loads(rows[0]["payload"])["mentions"]==(["owner"] if decision=="recommend" else [])


def test_nonqualifying_preview_can_be_reconsidered_when_counts_increase(factory,now):
    e,s,m,p,calls,meta=selection_engine(factory)
    current=[replace(meta,view_count=1,comment_count=0)]
    p.preview=lambda ref:(calls.append("preview") or current[0])
    assert e.browse(now)==0
    current[0]=replace(meta,view_count=9000,comment_count=30)
    assert e.browse(now+e.settings.interval_minutes*60+1)==1
    assert calls==["preview","preview","read","vision"]


def test_interest_exception_never_skips_sensitive_topic_filter(factory,now):
    e,s,m,p,calls,meta=selection_engine(factory)
    p.preview=lambda ref:(calls.append("preview") or replace(meta,title="galgame",tags=("政治",),view_count=0,comment_count=0))
    assert e.browse(now)==0 and calls==["preview"] and not m.calls


def test_explicit_owner_task_reads_low_volume_video_without_autonomous_preview(factory,now):
    e,s,m,p,calls,meta=selection_engine(factory)
    base=p.read
    p.read=lambda ref,**kw:replace(base(ref,**kw),view_count=0,comment_count=0)
    m.evaluation.update(decision="recommend")
    e.ingest(owner_message(now));assert e.process_one_event(now)
    assert e.run_due_task(now)
    assert "preview" not in calls and "read" in calls
    assert any(r["kind"]=="recommendation" for r in s.inspect("actions"))


def test_provider_without_preview_fails_before_full_read(factory,now):
    e,s,m,p,calls,meta=selection_engine(factory)
    p.preview=None
    assert e.browse(now)==0 and not calls and not m.calls


def test_owner_new_video_uses_real_author_metadata_and_can_comment_despite_old_only_flags(factory,now):
    e,s,m,p,calls,meta=selection_engine(factory)
    p.preview=lambda ref:(calls.append("preview") or replace(meta,author_id="424242",view_count=0,comment_count=0))
    e.ingest(Event("owner-upload","bilibili","robot","424242","owner-feed","owner_content","主人新视频",now,content=p.ref))
    assert e.process_one_event(now)
    assert calls==["preview","read","vision"]
    assert [r['kind'] for r in s.inspect('actions')]==['owner_comment']


def test_qq_explicit_private_video_task_ignores_popularity_but_keeps_private_delivery(factory,now):
    from test_qq_tasks import setup, request
    e,s,m,p,q=setup(factory,video_selection_mode='read_then_decide',min_video_views=8000,min_video_comments=20)
    original=p.read
    p.read=lambda *args,**kwargs:replace(original(*args,**kwargs),view_count=0,comment_count=0)
    p.preview=lambda ref:pytest.fail('explicit request must not use autonomous selection')
    request(e,now)
    assert e.run_due_task(now) and e.dispatch_one(now)
    assert len(q.sent)==1 and not p.sent
    assert q.sent[0].visibility=='private' and q.sent[0].recipient_id=='123'


def test_bilibili_preview_only_reads_view_and_tags_and_reuses_view_before_full_read(tmp_path):
    paths=[]
    def handle(request):
        paths.append(request.url.path)
        if request.url.path=="/x/web-interface/view":
            data={"aid":42,"bvid":"BVsynthetic","title":"普通标题","desc":"简介","owner":{"mid":123},"stat":{"view":1,"reply":0}}
        elif request.url.path=="/x/tag/archive/tags":data=[{"tag_name":"galgame"}]
        else:raise AssertionError("metadata preview requested media or comments")
        return httpx.Response(200,json={"code":0,"data":data})
    adapter=BilibiliAdapter(credentials(tmp_path),client=httpx.Client(transport=httpx.MockTransport(handle)),read_video_subtitles=False)
    adapter._comments=lambda *args:()
    ref=ContentRef("bilibili","video","42")
    try:
        meta=adapter.preview(ref)
        assert meta.evidence_level=="metadata" and not meta.images and not meta.comments
        assert meta.tags==("galgame",) and meta.view_count==1 and meta.comment_count==0
        assert paths==["/x/web-interface/view","/x/tag/archive/tags"]
        assert adapter.read(ref).tags==meta.tags
        assert len(paths)==2  # Same-runtime bounded metadata reuse.
    finally:adapter.close()


@pytest.mark.parametrize("code",[-509,-401,-500])
def test_tag_read_cooldown_or_auth_failure_cannot_be_treated_as_ordinary_missing_tags(tmp_path,code):
    def handle(request):
        if request.url.path.endswith('/view'):
            return httpx.Response(200,json={"code":0,"data":{"title":"普通标题"}})
        return httpx.Response(200,json={"code":code})
    adapter=BilibiliAdapter(credentials(tmp_path),client=httpx.Client(transport=httpx.MockTransport(handle)))
    try:
        with pytest.raises(PlatformError):adapter.preview(ContentRef("bilibili","video","42"))
    finally:adapter.close()


def test_confirmed_missing_tags_degrade_without_creating_a_keyword_exception(tmp_path):
    def handle(request):
        if request.url.path.endswith('/view'):
            return httpx.Response(200,json={"code":0,"data":{"title":"普通标题","stat":{"view":1,"reply":0}}})
        return httpx.Response(200,json={"code":-404})
    adapter=BilibiliAdapter(credentials(tmp_path),client=httpx.Client(transport=httpx.MockTransport(handle)))
    try:
        doc=adapter.preview(ContentRef('bilibili','video','42'))
        assert doc.tags==() and not interest_exempt(doc,VIDEO_INTEREST_KEYWORDS)
    finally:adapter.close()


def test_new_mode_config_defaults_and_keyword_validation(tmp_path):
    c=Config(write_config(tmp_path))
    assert c.behaviour().video_selection_mode=="read_then_decide"
    for patch in ({"video_selection_mode":[]},{"video_selection_mode":"bad"},{"video_interest_keywords":"galgame"},
                  {"video_interest_keywords":[""]},{"video_interest_keywords":[True]}):
        with pytest.raises(ValueError):c.save_local({"browsing":patch})
    c.save_local({"browsing":{"video_interest_keywords":["千恋万花","自定义厂牌"],"video_selection_mode":"legacy"}})
    assert Config(c.path).behaviour().video_interest_keywords==("千恋万花","自定义厂牌")


def test_final_policy_rechecks_keywords_thresholds_and_exact_explicit_sources(tmp_path,now):
    c=Config(write_config(tmp_path));c.save_local({"mode":"auto"})
    c=Config(c.path)
    p=TestPlatform();p.account_id,p.configured="robot",True;p.close=lambda:None
    rt=build(c,platform_factories={"bilibili":lambda c,s:BilibiliModule("bilibili",p,CAPABILITIES)})
    try:
        doc=replace(p.read(p.ref),title="普通标题",tags=("galgame",),view_count=1,comment_count=0,topic_class="casual")
        rt.store.cache_document(doc,now)
        a=Action("a",rt.store.agent_id,"bilibili","robot",p.ref,"合成评论",now,now+3600,kind="browse_comment",topic_class="casual")
        assert not rt.engine.action_policy(a,now)
        c.save_local({"browsing":{"video_interest_keywords":[]}})
        assert "门槛" in rt.engine.action_policy(a,now)
        ev=Event("real-inbound","bilibili","robot","123","thread","reply","聊聊",now,content=p.ref,reply_to="700")
        rt.store.enqueue_event(ev)
        reply=replace(a,kind="reply",event_id=ev.event_id,reply_to="700")
        assert not rt.engine.action_policy(reply,now)
        assert rt.engine.action_policy(replace(reply,reply_to="701"),now)
        binding=rt.store.binding(c.data['owner_person_id'],'bilibili')
        request=Event("task-inbound","bilibili","robot",binding['external_id'],"private-task","private_message","找视频",now,visibility="private")
        rt.store.enqueue_event(request)
        tid=rt.store.add_task(c.data['owner_person_id'],"合成任务",request.event_id,"",now,now,now+3600,binding['version'],target_platform='bilibili')
        task=rt.store.task(tid)
        task_action=replace(a,kind="recommendation",task_id=tid,task_version=task['version'],event_id=request.event_id)
        assert not rt.engine.action_policy(task_action,now)
        assert rt.engine.action_policy(replace(task_action,task_id='invented-task'),now)
    finally:rt.close()
