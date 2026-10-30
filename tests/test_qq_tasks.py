from dataclasses import replace
import json

import pytest

from murasamebot.application.task_routing import owner_video_route
from murasamebot.adapters.persistence.sqlite import SQLiteStore
from murasamebot.domain.models import Event, Receipt, Document


def setup(factory, **overrides):
    e,s,m,b=factory(**overrides)
    s.bind("owner","qq","123")
    class QQ:
        def __init__(self):self.sent=[]
        def allows_private_reply(self,account,peer):return account=="qq-bot" and peer=="123"
        def send(self,action,mentions,*,before_write=None):
            if before_write and not before_write():return Receipt("failed",detail="guarded")
            self.sent.append(action);return Receipt("accepted","qq-message")
    q=QQ();e.sinks["qq"]=q;e.private_access["qq"]=q
    e.private_policies["qq"]={"multilingual":True,"topic_filter":False}
    e.routing=lambda:(("bilibili","robot"),("qq","qq-bot"))
    e.request_route=lambda event,person:owner_video_route(s,event,person,provider="bilibili",private_platform="qq",public_platform="bilibili",accounts=dict(e.routing())) if event.platform=="qq" else None
    e.content_urls["bilibili"]=lambda ref:"https://www.bilibili.com/video/av"+ref.external_id
    original=m.decide
    def decide(op,payload):
        if op=="private_task_result":
            m.calls.append((op,payload))
            return {"decision":"recommend","text":"这期摄影入门很清楚。"+payload["video_url"],"can_send":True,"review_reason":"完整推荐正文通过"}
        return original(op,payload)
    m.decide=decide
    return e,s,m,b,q


def request(e,now, text="帮我找摄影入门视频，发到QQ",key="request"):
    event=Event(key,"qq","qq-bot","123","qq:private:owner","chat",text,now,"private")
    e.ingest(event)
    assert e.process_one_event(now)
    return event


def test_qq_request_searches_bilibili_and_delivers_private_link(factory,now):
    e,s,m,b,q=setup(factory)
    request(e,now)
    assert s.active_tasks("owner")[0]["route"]["provider"]=="bilibili"
    assert e.run_due_task(now)
    assert b.searches and not b.sent
    assert e.dispatch_one(now)
    assert len(q.sent)==1 and q.sent[0].recipient_id=="123"
    assert "https://www.bilibili.com/video/av10001" in q.sent[0].text
    assert s.inspect("tasks")[0]["state"]=="completed"
    assert s.inspect("conversation_records")[-1]["visibility"]=="private"


def test_explicit_bilibili_mention_keeps_private_source_out_of_public_review(factory,now):
    e,s,m,b,q=setup(factory,model_review=True,review_strategy="independent")
    m.evaluation.update(can_send=True,review_reason="通过")
    old=m.decide
    def review(op,p):
        if op=="review_action":
            m.calls.append((op,p));return {"decision":"approve","reason":"通过"}
        return old(op,p)
    m.decide=review
    request(e,now,"我的私人工作很烦，帮我找摄影视频，在B站@我")
    assert e.run_due_task(now)
    assert e.dispatch_one(now)
    assert b.sent[0][1]=={"owner":"424242"} and not q.sent
    payload=next(p for op,p in m.calls if op=="review_action")
    assert payload["source_message"] is None and "私人工作" not in json.dumps(payload,ensure_ascii=False)


def test_explicit_task_runs_with_general_browsing_off(factory,now):
    e,s,m,b,q=setup(factory,browsing_enabled=False)
    request(e,now)
    assert e.run_due_task(now)
    assert e.dispatch_one(now) and q.sent


@pytest.mark.parametrize("text",["不想看了","不用找了","第1条：不想看了"])
def test_cancel_without_model_intent_cancels_pending_result(factory,now,text):
    e,s,m,b,q=setup(factory)
    request(e,now);e.run_due_task(now)
    m.intent={"kind":"none"}
    request(e,now+1,text,"cancel")
    assert s.inspect("tasks")[0]["state"]=="cancelled"
    assert not e.dispatch_one(now+2) and not q.sent and not b.sent


def test_cancel_during_search_cannot_queue_old_result(factory,now):
    e,s,m,b,q=setup(factory)
    request(e,now)
    original=b.search
    def search(*args):
        request(e,now+1,"不想看了","cancel")
        return original(*args)
    b.search=search
    assert e.run_due_task(now)
    assert not e.dispatch_one(now+2)
    assert s.inspect("tasks")[0]["state"]=="cancelled" and not q.sent


def test_new_buffered_message_holds_delivery_until_cancel_is_processed(factory,now):
    from murasamebot.adapters.persistence.chat_batches import ChatBatches
    e,s,m,b,q=setup(factory)
    request(e,now);e.run_due_task(now)
    event=Event("cancel-buffer","qq","qq-bot","123","qq:private:owner","chat","不想看了",now+1,"private")
    inbox=ChatBatches(s);inbox.receive(event,s.binding("owner","qq"),now+1)
    assert not e.dispatch_one(now+2)
    assert inbox.flush({"robot_id":"qq-bot","quiet_seconds":30,"max_wait_seconds":180},"owner",lambda x:x,now+31)
    assert not e.dispatch_one(now+31)  # now waiting in the authoritative event queue
    assert e.process_one_event(now+31)
    assert s.inspect("tasks")[0]["state"]=="cancelled"
    assert not q.sent


def test_cancel_at_final_send_gate_blocks_the_write(factory,now):
    e,s,m,b,q=setup(factory)
    request(e,now);e.run_due_task(now)
    original=q.send
    def send(action,mentions,*,before_write=None):
        request(e,now+1,"不想看了","cancel")
        return original(action,mentions,before_write=before_write)
    q.send=send
    assert e.dispatch_one(now)
    assert not q.sent and s.inspect("tasks")[0]["state"]=="cancelled"


def test_missing_provider_waits_without_losing_or_spending_attempts(factory,now):
    e,s,m,b,q=setup(factory)
    request(e,now);e.providers={}
    assert not e.run_due_task(now)
    assert s.active_tasks("owner")[0]["attempts"]==0
    e.providers={"bilibili":b}
    assert e.run_due_task(now)


def test_restart_preserves_route_and_dedup(factory,now):
    e,s,m,b,q=setup(factory)
    request(e,now)
    reopened=SQLiteStore(s.path,s.agent_id)
    assert reopened.active_tasks("owner")[0]["route"]["platform"]=="qq"
    e.store=reopened
    assert e.run_due_task(now) and e.dispatch_one(now)
    assert not e.run_due_task(now+1) and len(q.sent)==1


def test_revocation_and_forged_recipient_block_result(factory,now):
    e,s,m,b,q=setup(factory)
    request(e,now);e.run_due_task(now)
    a=next(r for r in s.inspect("actions") if r["kind"]=="task_result")
    from murasamebot.domain.models import Action
    action=Action.from_dict(json.loads(a["payload"]))
    assert s.validate_action(replace(action,recipient_id="stranger"),now)=="task_private_route_mismatch"
    s.revoke_binding("owner","qq")
    assert not e.dispatch_one(now) and not q.sent


def test_update_replaces_task_and_pending_result(factory,now):
    e,s,m,b,q=setup(factory)
    request(e,now);e.run_due_task(now)
    m.intent={"kind":"update","topic":"夜景摄影","sensitive":False}
    request(e,now+1,"换成夜景摄影的视频发到QQ","update")
    assert len(s.active_tasks("owner"))==1 and s.active_tasks("owner")[0]["topic"]=="夜景摄影"
    assert s.inspect("actions")[0]["state"]=="cancelled"


def test_ordinary_private_conversation_does_not_become_recurring_task(factory,now):
    e,s,m,b,q=setup(factory)
    m.intent={"kind":"none"}
    request(e,now,"今天只是随便聊聊")
    assert not s.active_tasks("owner")


def test_real_private_message_ack_and_task_result_are_separate_reviewed_actions(factory,now):
    e,s,m,b,q=setup(factory,model_review=True,review_strategy="inline")
    old=m.decide
    def decide(op,p):
        if op=="private_chat":
            m.calls.append((op,p));return {"decision":"reply","text":"吾辈已经记下找视频的委托了。","can_send":True,"review_reason":"通过"}
        return old(op,p)
    m.decide=decide
    event=Event("private-request","qq","qq-bot","123","qq:private:owner","private_message","找摄影视频发这里",now,"private")
    e.ingest(event);assert e.process_one_event(now)
    assert e.run_due_task(now)
    assert e.dispatch_one(now) and e.dispatch_one(now+2)
    assert [a.kind for a in q.sent]==["private_reply","task_result"]
    assert all(a.review_state=="passed" for a in q.sent)
    chat=next(p for op,p in m.calls if op=="private_chat")
    assert chat["intent_result"]["delivery_platform"]=="qq"


@pytest.mark.parametrize("message,platform",[("找摄影视频，别@我，发到QQ","qq"),("找视频，不用发到QQ，在B站@我","bilibili")])
def test_negated_delivery_request_is_respected(factory,now,message,platform):
    e,s,m,b,q=setup(factory)
    request(e,now,message)
    assert s.active_tasks("owner")[0]["route"]["platform"]==platform


def test_other_private_chat_does_not_block_or_cancel_owner_task(factory,now):
    e,s,m,b,q=setup(factory)
    request(e,now);e.run_due_task(now)
    e.ingest(Event("other","qq","qq-bot","stranger","room","private_message","不想看了",now+1,"private"))
    assert e.dispatch_one(now+2) and q.sent


def test_missing_private_source_in_public_review_is_preserved_on_rejection(factory,now):
    e,s,m,b,q=setup(factory,model_review=True)
    old=m.decide
    m.decide=lambda op,p: {"decision":"reject","reason":"不合适"} if op=="review_action" else old(op,p)
    request(e,now,"找视频，在B站@我")
    assert e.run_due_task(now)
    assert e.dispatch_one(now) and not b.sent
    assert s.inspect("actions")[0]["state"]=="draft"


def test_result_waits_outside_configured_window(factory,now):
    e,s,m,b,q=setup(factory,browsing_enabled=False)
    request(e,now);e.run_due_task(now)
    e.settings=replace(e.settings,windows=(("01:00","02:00"),))
    assert not e.dispatch_one(now)
    assert s.inspect("actions")[0]["state"]=="queued"
    assert s.active_tasks("owner")[0]["state"]=="awaiting_approval"


def test_private_result_cannot_add_untrusted_link(factory,now):
    e,s,m,b,q=setup(factory)
    old=m.decide
    def decide(op,p):
        result=old(op,p)
        if op=="private_task_result":result["text"]+=" https://evil.invalid/"
        return result
    m.decide=decide
    request(e,now);assert e.run_due_task(now)
    assert not s.inspect("actions") and not q.sent


def test_existing_public_comment_does_not_suppress_private_share(factory,now):
    from murasamebot.domain.models import Action
    e,s,m,b,q=setup(factory)
    s.enqueue_action(Action("existing",s.agent_id,"bilibili","robot",b.ref,"旧评论",now,now+3600),draft=True,now=now)
    request(e,now);assert e.run_due_task(now)
    assert any(r["kind"]=="task_result" for r in s.inspect("actions"))


def test_private_sensitive_video_uses_private_provider_and_preserves_original_language(factory,now):
    e,s,m,b,q=setup(factory)
    m.intent.update(topic="政治分析",sensitive=True)
    class PrivateProvider:
        searches=[]
        def search(self,*args):self.searches.append(args);return [b.ref]
        def read(self,ref,**kwargs):
            return Document(ref,"政治分析と解説","实际取得的视频简介。","text",topic_class="sensitive")
    private=PrivateProvider();e.private_content_providers["bilibili"]=private
    def denied(*args,**kwargs):raise AssertionError("private request used public provider")
    b.search=denied;b.read=denied
    old=m.decide
    def decide(op,p):
        if op=="private_task_result":
            m.calls.append((op,p))
            return {"decision":"recommend","text":"【解説】Politics analysis と解説 "+p["video_url"],"can_send":True,"review_reason":"符合私人委托"}
        return old(op,p)
    m.decide=decide
    request(e,now,"找这个主题的视频，发到QQ")
    assert e.run_due_task(now) and private.searches
    assert e.dispatch_one(now) and q.sent and not b.sent
    assert "と解説" in q.sent[0].text and s.inspect("tasks")[0]["state"]=="completed"
    policy=next(p["private_policy"] for op,p in m.calls if op=="private_task_result")
    assert policy=={"multilingual":True,"topic_filter":False}


@pytest.mark.parametrize("flag",[True,False])
def test_qq_private_policy_cannot_relax_public_bilibili_task(factory,now,flag):
    e,s,m,b,q=setup(factory)
    m.intent["sensitive"]=flag
    b.documents[b.ref.key]=Document(b.ref,"政治分析","资料","text",topic_class="sensitive")
    request(e,now,"找这个主题的视频，在B站@我")
    assert e.run_due_task(now)
    assert not s.inspect("actions") and not b.sent and not q.sent


def test_multilingual_private_result_independent_review_receives_private_policy(factory,now):
    e,s,m,b,q=setup(factory,model_review=True,review_strategy="independent")
    old=m.decide
    def decide(op,p):
        if op=="review_action":
            m.calls.append((op,p));return {"decision":"approve","reason":"私人回复语言可混合"}
        result=old(op,p)
        if op=="private_task_result":result["text"]="ムラサメ best moments "+p["video_url"]
        return result
    m.decide=decide
    request(e,now);assert e.run_due_task(now) and e.dispatch_one(now)
    assert q.sent and next(p for op,p in m.calls if op=="review_action")["private_policy"]["multilingual"]


def test_real_module_assembly_limits_relaxed_policy_to_qq(tmp_path):
    import yaml
    from murasamebot.bootstrap.config import Config
    from murasamebot.bootstrap.runtime import build
    from test_architecture_and_config import write_config
    path=write_config(tmp_path)
    Config(path).local_path.write_text(yaml.safe_dump({"platforms":{"qq":{"enabled":True}}}),encoding="utf-8")
    runtime=build(Config(path))
    try:
        assert runtime.engine.private_policies=={"qq":{"multilingual":True,"topic_filter":False}}
        assert "bilibili" in runtime.engine.private_content_providers
    finally:runtime.close()


def test_private_confirmation_reports_explicit_search_available_when_browsing_is_off(factory,now):
    e,s,m,b,q=setup(factory,browsing_enabled=False)
    seen=[];old=m.decide
    def decide(op,p):
        if op=="private_chat":
            seen.append(p);return {"decision":"reply","text":"记下啦，到约好的时间吾辈会帮主人找。"}
        return old(op,p)
    m.decide=decide
    event=Event("confirmation","qq","qq-bot","123","qq:private:owner","private_message","十分钟后找视频",now,"private")
    e.ingest(event);assert e.process_one_event(now)
    assert seen[0]["intent_result"]["status"]=="saved"
    assert seen[0]["explicit_video_requests_enabled"] and not seen[0]["browsing_enabled"]
    assert e.dispatch_one(now) and q.sent
