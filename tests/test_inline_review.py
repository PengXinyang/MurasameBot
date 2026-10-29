from dataclasses import replace
import json

import pytest

from murasamebot.application.expressions import ExpressionService, ExpressionSettings, KAOMOJIS
from murasamebot.bootstrap.config import Config
from murasamebot.bootstrap.runtime import build
from murasamebot.domain.models import Action, Event
from test_architecture_and_config import write_config
from test_artwork import discovery
from test_model_review import inbound, review_model
from test_private_messages import chat_engine, private_event


def verdict(**extra):
    return {"can_send":True,"review_reason":"符合当前语境，无需人工复核",**extra}


def only_action(store):
    row=store.inspect("actions")[0]
    return row,Action.from_dict(json.loads(row["payload"]))


def test_product_default_is_same_generation_judgement(tmp_path):
    config=Config(write_config(tmp_path))
    assert config.data["review"]["strategy"]=="inline"
    assert config.behaviour().review_strategy=="inline" and config.behaviour().model_review


def test_inline_reply_generates_once_and_sender_never_calls_model(factory,now):
    engine,store,model,platform=factory(model_review=True,review_strategy="inline")
    model.reply.update(verdict())
    engine.ingest(inbound(now));engine.process_one_event(now)
    row,action=only_action(store)
    assert row["state"]=="queued" and action.review_state=="passed" and action.review_method=="inline"
    assert not engine.review_one(now)
    assert engine.dispatch_one(now,pre_reviewed_only=True)
    assert [op for op,_ in model.calls]==["reply"]
    assert len(platform.sent)==1


@pytest.mark.parametrize("fields,state",[
    ({"can_send":False,"review_reason":"需要主人确认语气"},"rejected"),
    ({},"unavailable"),({"can_send":"true","review_reason":"通过"},"unavailable"),
    ({"can_send":1,"review_reason":"通过"},"unavailable"),
    ({"can_send":True},"unavailable"),({"can_send":True,"review_reason":""},"unavailable"),
    ({"can_send":True,"review_reason":{}},"unavailable")])
def test_false_or_malformed_verdict_goes_directly_to_human_without_extra_request(factory,now,fields,state):
    engine,store,model,platform=factory(model_review=True,review_strategy="inline")
    model.reply.update(fields)
    engine.ingest(inbound(now));engine.process_one_event(now)
    row,action=only_action(store)
    assert row["state"]=="draft" and action.review_state==state
    assert not engine.review_one(now) and not engine.dispatch_one(now)
    assert [op for op,_ in model.calls]==["reply"] and not platform.sent
    store.approve_action(action.action_id,now+1)
    assert engine.dispatch_one(now+2,pre_reviewed_only=True)
    assert [op for op,_ in model.calls]==["reply"]


def test_private_inline_judgement_never_overrides_lost_mutual_relationship(factory,now):
    engine,store,model,platform,access=chat_engine(factory,model_review=True,review_strategy="inline")
    original=model.decide
    def decide(op,payload):
        result=original(op,payload)
        return {**result,**verdict()} if op=="private_chat" else result
    model.decide=decide
    engine.ingest(private_event(now,peer="555"));engine.process_one_event(now)
    assert only_action(store)[1].review_state=="passed"
    access.allowed=False
    assert engine.dispatch_one(now,pre_reviewed_only=True)
    assert not platform.sent and only_action(store)[0]["state"]=="failed"
    assert [op for op,_ in model.calls]==["private_chat"]


@pytest.mark.parametrize("separate_review_worker",[True,False])
def test_old_unreviewed_queue_is_drafted_without_silent_second_model_call(factory,now,separate_review_worker):
    engine,store,model,platform=factory()
    engine.ingest(inbound(now));engine.process_one_event(now)
    engine.settings=replace(engine.settings,model_review=True,review_strategy="inline")
    if separate_review_worker:
        assert not engine.dispatch_one(now,pre_reviewed_only=True)
        assert engine.review_one(now)
    else:
        assert engine.dispatch_one(now)
    assert only_action(store)[0]["state"]=="draft"
    assert [op for op,_ in model.calls]==["reply"] and not platform.sent


def test_switch_to_independent_requires_fresh_independent_approval(factory,now):
    engine,store,model,platform=factory(model_review=True,review_strategy="inline")
    model.reply.update(verdict())
    engine.ingest(inbound(now));engine.process_one_event(now)
    assert store.queue_status(review_strategy="inline")["review_ready_actions"]==0
    assert store.queue_status(review_strategy="independent")["review_ready_actions"]==1
    engine.settings=replace(engine.settings,review_strategy="independent")
    assert not engine.dispatch_one(now,pre_reviewed_only=True)
    review_model(model)
    assert engine.review_one(now)
    assert only_action(store)[1].review_method=="independent"
    assert engine.dispatch_one(now,pre_reviewed_only=True)
    assert [op for op,_ in model.calls]==["reply","review_action"] and len(platform.sent)==1


def test_draft_mode_still_needs_human_even_when_generator_says_yes(factory,now):
    engine,store,model,platform=factory(model_review=False,review_strategy="inline",draft=True)
    model.reply.update(verdict())
    engine.ingest(inbound(now));engine.process_one_event(now)
    assert only_action(store)[0]["state"]=="draft"
    assert not engine.dispatch_one(now) and not platform.sent


def test_inline_judgement_binds_actual_native_or_kaomoji_rendering(factory,now):
    engine,store,model,_=factory(model_review=True,review_strategy="inline")
    engine.expressions=ExpressionService(ExpressionSettings(),{},store)
    model.reply.update(verdict(emojis=[KAOMOJIS[0].key]))
    engine.ingest(inbound(now));engine.process_one_event(now)
    _,action=only_action(store)
    assert action.review_state=="passed" and action.text.endswith(KAOMOJIS[0].token)
    assert engine.dispatch_one(now,pre_reviewed_only=True)
    assert len(model.calls)==1


def test_inline_render_normalization_cannot_reuse_approval_for_different_text(factory,now):
    engine,store,model,_=factory(model_review=True,review_strategy="inline")
    engine.expressions=ExpressionService(ExpressionSettings(),{},store)
    model.reply.update(verdict(text="吾辈明白啦[模型内联的表情]",emojis=[]))
    engine.ingest(inbound(now));engine.process_one_event(now)
    row,action=only_action(store)
    assert row["state"]=="draft" and action.review_state=="unavailable"
    assert not engine.review_one(now) and len(model.calls)==1


def test_video_comment_and_extra_reply_have_independent_verdicts(factory,now):
    engine,store,model,platform=factory(model_review=True,review_strategy="inline")
    model.evaluation={"decision":"comment","text":"吾辈想先练练构图。",**verdict(),
        "comment_reply":{"reply_to":"700","text":"这个问题还需要确认。",**verdict(can_send=False,review_reason="回答依据不够")}}
    assert engine._evaluate(platform.read(platform.ref),now)
    rows=store.inspect("actions")
    comment=next(r for r in rows if r["kind"]=="browse_comment")
    extra=next(r for r in rows if r["kind"]=="browse_reply")
    assert comment["state"]=="queued" and extra["state"]=="draft"
    assert [op for op,_ in model.calls]==["evaluate_video"]


def test_extra_reply_missing_its_own_verdict_is_not_approved_by_parent(factory,now):
    engine,store,model,platform=factory(model_review=True,review_strategy="inline")
    model.evaluation={"decision":"comment","text":"吾辈想先练练构图。",**verdict(),
        "comment_reply":{"reply_to":"700","text":"可以先试试固定场景。"}}
    engine._evaluate(platform.read(platform.ref),now)
    extra=next(r for r in store.inspect("actions") if r["kind"]=="browse_reply")
    assert extra["state"]=="draft"


@pytest.mark.parametrize("decision",["reply","recommend"])
def test_browsing_root_decisions_use_the_inline_verdict(factory,now,decision):
    engine,store,model,platform=factory(model_review=True,review_strategy="inline")
    model.evaluation={"decision":decision,"text":"吾辈觉得这个思路值得一试。","reply_to":"700",**verdict()}
    assert engine._evaluate(platform.read(platform.ref),now)
    assert only_action(store)[1].review_method=="inline"


def test_owner_repost_uses_generation_verdict(factory,now):
    engine,store,model,platform=factory(model_review=True,review_strategy="inline")
    original=model.decide
    def decide(op,payload):
        if op=="owner_repost":
            model.calls.append((op,payload))
            return {"decision":"share","topic_class":"casual","text":"主人更新啦，吾辈来分享。",**verdict()}
        return original(op,payload)
    model.decide=decide
    document=replace(platform.read(platform.ref),author_id="424242")
    platform.documents[platform.ref.key]=document
    event=Event("repost","bilibili","robot","424242","owner-space","owner_repost","",now,content=platform.ref)
    engine.ingest(event);engine.process_one_event(now)
    assert only_action(store)[1].review_state=="passed"
    assert [op for op,_ in model.calls]==["owner_repost"]


def test_art_caption_uses_same_generation_judgement(factory,now):
    engine,store,model,_=factory()
    service,calls,_=discovery(engine,store,model)
    original=model.decide
    model.decide=lambda op,payload:{**original(op,payload),**verdict()}
    assert service.run(now,agent_id=store.agent_id,platform="bilibili",account_id="robot",timezone="Asia/Hong_Kong",draft=False,ttl_minutes=30,review_strategy="inline")
    assert only_action(store)[1].review_state=="passed" and len(calls)==1


def test_review_strategy_hot_reload_cannot_reuse_inline_approval(tmp_path):
    config=Config(write_config(tmp_path));runtime=build(config)
    try:
        action=Action("a",runtime.store.agent_id,"bilibili","robot",None,"吾辈来了。",1000,2000,review_state="passed",review_method="inline")
        config.save_local({"review":{"strategy":"independent"}})
        assert "审核策略已经变化" in runtime.engine.action_policy(action,1001)
    finally:
        runtime.close()
