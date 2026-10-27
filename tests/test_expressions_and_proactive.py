from dataclasses import replace
import json
from urllib.parse import parse_qs

import httpx
import pytest

from murasamebot.adapters.platforms.bilibili import BilibiliAdapter
from murasamebot.application.expressions import ExpressionService, ExpressionSettings, UNICODE_EMOJIS
from murasamebot.domain.models import Action, ContentRef, Document, Emoji, PlatformError, PolicyError
from test_bilibili_contracts import credentials
from test_model_review import inbound, review_model
from test_private_messages import chat_engine, private_event


CONGYU = Emoji("bilibili:102084", "[千恋万花表情包_丛雨Ciallo]", "丛雨Ciallo")
SMILE = Emoji("bilibili:1", "[微笑]", "微笑")


class Catalog:
    def __init__(self):
        self.calls = 0

    def emoji_options(self, account_id, channel):
        self.calls += 1
        return (SMILE, CONGYU)


def with_expressions(engine, store, **kw):
    catalog = Catalog()
    engine.expressions = ExpressionService(ExpressionSettings(**kw), {"bilibili":catalog}, store)
    return catalog


def test_video_comment_and_comment_reply_are_independent_and_deduplicated(factory, now):
    engine, store, model, platform = factory()
    with_expressions(engine, store)
    model.evaluation = {"decision":"comment","text":"吾辈觉得曝光这部分值得先练。","emojis":[CONGYU.key],
                        "comment_reply":{"reply_to":"700","text":"可以先固定场景练习，再比较效果。","emojis":[SMILE.key]}}
    doc = platform.read(platform.ref)
    engine.document(platform.ref, now)
    assert engine._evaluate(doc, now)
    rows = store.inspect("actions")
    assert len(rows) == 2 and {r["kind"] for r in rows} == {"browse_comment","browse_reply"}
    reply = Action.from_dict(json.loads(next(r["payload"] for r in rows if r["kind"] == "browse_reply")))
    assert reply.reply_to == "700" and reply.thread_root == "600" and reply.emojis == (SMILE,)
    assert reply.text.endswith(SMILE.token)
    engine._evaluate(doc, now+1)
    assert len(store.inspect("actions")) == 2


def test_comment_reply_can_be_chosen_without_video_comment(factory, now):
    engine, store, model, platform = factory()
    model.evaluation = {"decision":"skip","comment_reply":{"reply_to":"700","text":"吾辈建议先做一个简单对照练习。"}}
    assert not engine._evaluate(platform.read(platform.ref),now)
    assert [r["kind"] for r in store.inspect("actions")] == ["browse_reply"]
    assert not store.has_action_for_content(platform.ref.key,now)


@pytest.mark.parametrize("decision,overrides", [("comment",{"comment_videos":False}), ("reply",{"reply_comments":False}), ("recommend",{"recommend_owner":False})])
def test_disabled_proactive_mode_cannot_be_overridden_by_model(factory, now, decision, overrides):
    engine,store,model,platform=factory(**overrides)
    model.evaluation={"decision":decision,"text":"吾辈觉得这个主题很有意思。","reply_to":"700"}
    assert not engine._evaluate(platform.read(platform.ref),now)
    assert not store.inspect("actions")


def test_cannot_reply_to_own_or_invented_comment(factory,now):
    engine,store,model,platform=factory()
    doc=replace(platform.read(platform.ref),comments=({"id":"700","root":"700","text":"吾辈的评论","author_id":"robot"},))
    model.evaluation={"decision":"reply","text":"吾辈来回复。","reply_to":"700"}
    with pytest.raises(PolicyError):
        engine._evaluate(doc,now)
    assert not store.inspect("actions")
    assert model.calls[-1][1]["content"]["comments"] == ()


def test_extra_comment_reply_cannot_complete_owner_task(factory,now):
    from conftest import owner_message
    engine,store,model,platform=factory()
    engine.ingest(owner_message(now));engine.process_one_event(now)
    task=store.claim_task(now)
    model.evaluation={"decision":"skip","comment_reply":{"reply_to":"700","text":"吾辈赞同这个看法。"}}
    assert not engine._evaluate(platform.read(platform.ref),now,task=task)
    assert not store.inspect("actions")


def test_catalog_prioritizes_congyu_and_caches_by_account(factory,now):
    engine,store,_,_=factory()
    catalog=with_expressions(engine,store)
    options=engine.expressions.options("bilibili","robot","public",now)
    assert options[0]==CONGYU
    engine.expressions.options("bilibili","robot","public",now+1)
    assert catalog.calls==1
    engine.expressions.options("bilibili","other-account","public",now+1)
    assert catalog.calls==2


def test_catalog_failure_keeps_plain_text_and_backs_off(factory,now):
    engine,store,_,_=factory()
    class Broken:
        calls=0
        def emoji_options(self,*args):
            self.calls+=1
            raise PlatformError("unavailable")
    source=Broken()
    engine.expressions=ExpressionService(ExpressionSettings(),{"bilibili":source},store)
    assert engine.expressions.options("bilibili","robot","public",now)==()
    assert engine.expressions.options("bilibili","robot","public",now+1)==()
    assert source.calls==1


def test_inline_and_invented_emoji_cannot_exceed_two(factory):
    engine,store,_,_=factory()
    with_expressions(engine,store)
    result={"text":"吾辈明白啦😊😊😊[虚构表情][微笑]","emojis":[CONGYU.key,SMILE.key,UNICODE_EMOJIS[0].key,"invented"]}
    text,emojis=engine.message_text(result,(CONGYU,SMILE,*UNICODE_EMOJIS),250)
    assert text=="吾辈明白啦"+CONGYU.token+SMILE.token and len(emojis)==2


def test_expression_choice_can_be_empty_or_disabled(factory):
    engine,store,_,_=factory()
    with_expressions(engine,store)
    assert engine.message_text({"text":"吾辈知道了。","emojis":[]},(CONGYU,),250)==("吾辈知道了。",())
    with_expressions(engine,store,enabled=False)
    assert engine.message_text({"text":"吾辈知道了😊","emojis":[CONGYU.key]},(CONGYU,),250)==("吾辈知道了",())


def test_emoji_is_omitted_instead_of_overflowing_character_budget(factory):
    engine,store,_,_=factory()
    with_expressions(engine,store)
    text,emojis=engine.message_text({"text":"吾辈知道了","emojis":[CONGYU.key]},(CONGYU,),6)
    assert text=="吾辈知道了" and not emojis


def test_public_reply_and_review_include_same_verified_emojis(factory,now):
    engine,store,model,platform=factory(model_review=True)
    with_expressions(engine,store)
    model.reply={"decision":"reply","text":"吾辈也觉得挺有趣。","emojis":[CONGYU.key]}
    review_model(model)
    engine.ingest(inbound(now));engine.process_one_event(now);engine.review_one(now)
    payload=next(p for op,p in model.calls if op=="review_action")
    assert payload["proposed_text"].endswith(CONGYU.token)
    assert payload["emojis"][0]["key"]==CONGYU.key
    row=store.inspect("actions")[0]
    assert Action.from_dict(json.loads(row["payload"])).emojis==(CONGYU,)


def test_private_chat_uses_channel_appropriate_unicode_expressions(factory,now):
    engine,store,model,platform,access=chat_engine(factory)
    with_expressions(engine,store)
    original=model.decide
    def decide(op,payload):
        if op=="private_chat":
            assert all(e["key"].startswith("unicode:") for e in payload["emoji_options"])
            return {"decision":"reply","text":"吾辈陪你聊一会儿。","emojis":[UNICODE_EMOJIS[0].key]}
        return original(op,payload)
    model.decide=decide
    engine.ingest(private_event(now));engine.process_one_event(now)
    action=Action.from_dict(json.loads(store.inspect("actions")[0]["payload"]))
    assert action.text.endswith("😊") and action.visibility=="private"


def test_bilibili_catalog_filters_access_and_expiry_but_not_unlocked_flag(tmp_path):
    emote={"id":102084,"text":CONGYU.token,"meta":{"alias":"丛雨Ciallo"},"flags":{"unlocked":False}}
    packs=[{"text":"千恋万花表情包","flags":{"added":True},"emote":[emote]},
           {"flags":{"no_access":True},"emote":[{**emote,"id":2}]},
           {"expire_time":1,"emote":[{**emote,"id":3}]},
           {"emote":[{**emote,"id":4,"flags":{"no_access":True}}]}]
    def handle(request):
        data={"isLogin":True,"mid":999} if request.url.path.endswith("nav") else {"packages":packs}
        return httpx.Response(200,json={"code":0,"data":data})
    adapter=BilibiliAdapter(credentials(tmp_path),client=httpx.Client(transport=httpx.MockTransport(handle)))
    assert adapter.emoji_options("999","public")== (CONGYU,)


def test_native_comment_encoding_and_revoked_emoji_blocks_send(tmp_path):
    posts=[]; revoked=[False]
    def handle(request):
        if request.url.path.endswith("nav"):
            data={"isLogin":True,"mid":999}
        elif request.url.path.endswith("panel/web"):
            data={"packages":[{"emote":[{"id":102084,"text":CONGYU.token,"meta":{"alias":"丛雨Ciallo"},"flags":{"no_access":revoked[0]}}]}]}
        else:
            posts.append(request)
            data={"reply":{"rpid":123}}
        return httpx.Response(200,json={"code":0,"data":data})
    adapter=BilibiliAdapter(credentials(tmp_path),client=httpx.Client(transport=httpx.MockTransport(handle)))
    action=Action("a","agent","bilibili","999",ContentRef("bilibili","video","42"),"吾辈来了。"+CONGYU.token,1000,2000,emojis=(CONGYU,))
    assert adapter.send(action,{}).state=="accepted"
    assert parse_qs(posts[0].content.decode())["message"]==[action.text]
    revoked[0]=True
    assert adapter.send(action,{}).state=="failed" and len(posts)==1


def test_zero_root_is_normalized_and_own_comments_not_candidates(tmp_path):
    rows=[{"rpid_str":"700","root_str":"0","member":{"mid":123},"content":{"message":"想听听看法"}},
          {"rpid_str":"701","member":{"mid":999},"content":{"message":"机器人自己的评论"}}]
    adapter=BilibiliAdapter(credentials(tmp_path),client=httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={"code":0,"data":{"replies":rows}}))))
    comments=adapter._comments(ContentRef("bilibili","video","42"),"","")
    assert len(comments)==1 and comments[0]["id"]==comments[0]["root"]=="700"


def test_proactive_thread_reply_uses_verified_root_parent_and_review_context(factory,now):
    engine,store,model,platform=factory(model_review=True)
    review_model(model)
    model.evaluation={"decision":"reply","reply_to":"700","text":"吾辈觉得可以先固定场景练习。"}
    doc=engine.document(platform.ref,now)
    assert engine._evaluate(doc,now)
    assert engine.review_one(now)
    review=next(payload for op,payload in model.calls if op=="review_action")
    assert review["target_comment"]["id"]=="700"
    assert engine.dispatch_one(now,pre_reviewed_only=True)
    action=platform.sent[0][0]
    assert action.kind=="browse_reply" and action.reply_to=="700" and action.thread_root=="600"


def test_dynamic_emojis_are_encoded_as_rich_text_nodes(tmp_path):
    posts=[]
    def handle(request):
        if request.url.path.endswith("nav"):
            data={"isLogin":True,"mid":999}
        elif request.url.path.endswith("panel/web"):
            assert request.url.params["business"]=="dynamic"
            data={"packages":[{"emote":[{"id":102084,"text":CONGYU.token,"meta":{"alias":"丛雨Ciallo"}}]}]}
        elif request.url.path.endswith("detail"):
            data={"item":{"modules":{"module_author":{"mid":424242}}}}
        else:
            posts.append(json.loads(request.content));data={"dyn_id_str":"777"}
        return httpx.Response(200,json={"code":0,"data":data})
    adapter=BilibiliAdapter(credentials(tmp_path),client=httpx.Client(transport=httpx.MockTransport(handle)))
    action=Action("share","agent","bilibili","999",ContentRef("bilibili","post","900"),"主人更新啦。"+CONGYU.token,1000,2000,
                  kind="owner_repost",source_author_id="424242",emojis=(CONGYU,))
    assert adapter.send(action,{}).state=="accepted"
    assert posts[0]["dyn_req"]["content"]["contents"]==[
        {"raw_text":"主人更新啦。","type":1,"biz_id":""},{"raw_text":CONGYU.token,"type":9,"biz_id":""}]
