import json
from dataclasses import replace

import httpx
import pytest

from murasamebot.adapters.platforms.qq import QQAdapter
from murasamebot.adapters.persistence.chat_batches import ChatBatches
from murasamebot.bootstrap.platforms.qq import DEFAULTS, QQModule
from murasamebot.domain.models import Receipt


def setup(factory, *, timeout=False):
    engine,store,model,platform = factory()
    store.bind("owner","qq","123")
    settings={**DEFAULTS,"robot_id":"456","access_token":"test","callback_secret":"test"}
    writes=[]
    def handle(request):
        if request.url.path=="/get_login_info": data={"user_id":456}
        elif request.url.path=="/get_friend_list": data=[{"user_id":123}]
        else:
            writes.append(json.loads(request.content))
            if timeout:raise httpx.ReadTimeout("lost")
            data={"message_id":789}
        return httpx.Response(200,json={"status":"ok","retcode":0,"data":data})
    adapter=QQAdapter(settings,store,"owner",client=httpx.Client(base_url="http://127.0.0.1:3000",transport=httpx.MockTransport(handle)))
    return engine,store,adapter,writes


def raw(mid=1, **kwargs):
    return {"post_type":"message","message_type":"private","sub_type":"friend","self_id":456,
        "user_id":123,"message_id":mid,"message":[{"type":"text","data":{"text":"你好"}}],**kwargs}


def test_reset_restart_and_dedup(factory,now):
    e,s,a,w=setup(factory); inbox=ChatBatches(s); module=QQModule("qq",a)
    event=a.parse(raw(),now); binding=s.binding("owner","qq")
    assert inbox.receive(event,binding,now)
    assert not inbox.receive(event,binding,now+29)
    assert not inbox.flush(a.settings,"owner",module.normalize_event,now+29)
    assert inbox.receive(a.parse(raw(2),now+20),binding,now+20)
    assert not inbox.flush(a.settings,"owner",module.normalize_event,now+31)
    assert ChatBatches(s).flush(a.settings,"owner",module.normalize_event,now+50)
    event=s.claim_event(now+50)
    assert event.text.count("你好")==2
    assert not inbox.flush(a.settings,"owner",module.normalize_event,now+100)
    assert not inbox.receive(a.parse(raw(1),now+100),binding,now+100)
    a.close()


def test_revoke_discards_unflushed_messages(factory,now):
    e,s,a,w=setup(factory); inbox=ChatBatches(s)
    inbox.receive(a.parse(raw(),now),s.binding("owner","qq"),now)
    s.revoke_binding("owner","qq")
    assert not inbox.flush(a.settings,"owner",lambda x:x,now+40)
    assert s.inspect("events")==[]
    a.close()


def test_forgetting_memory_invalidates_pending_buffer(factory,now):
    e,s,a,w=setup(factory);inbox=ChatBatches(s)
    event=a.parse(raw(),now)
    inbox.receive(event,s.binding("owner","qq"),now)
    s.set_setting("memory_epoch",s.get_setting("memory_epoch",0)+1)
    assert not inbox.flush(a.settings,"owner",lambda x:x,now+31)
    assert not inbox.receive(event,s.binding("owner","qq"),now+40)
    assert not s.inspect("events")
    a.close()


@pytest.mark.parametrize("changes",[{"user_id":999},{"self_id":999},{"message_type":"group"},{"sub_type":"group"}])
def test_deny_untrusted_context(factory,now,changes):
    e,s,a,w=setup(factory)
    assert a.parse(raw(**changes),now) is None
    assert not w
    a.close()


@pytest.mark.parametrize("host",["multimedia.nt.qq.com","multimedia.nt.qq.com.cn"])
def test_image_array_and_image_host_filter(factory,now,host):
    e,s,a,w=setup(factory)
    event=a.parse(raw(message=[{"type":"image","data":{"url":f"https://{host}/a.png"}},
        {"type":"image","data":{"url":"file:///private.txt"}}]),now)
    assert len(event.images)==1 and "不可读取" in event.text
    assert type(event).from_dict(event.to_dict())==event
    a.close()


def test_owner_private_multiple_messages_use_shared_engine(factory,now):
    e,s,a,w=setup(factory)
    e.private_access["qq"]=a;e.sinks["qq"]=a
    e.settings=replace(e.settings,model_review=True,review_strategy="inline",draft=False)
    original=e.model.decide
    e.model.decide=lambda op,payload: {"decision":"reply","messages":["吾辈在这里。","你想说什么呀？"],"can_send":True,"review_reason":"全部正文已审核"} if op=="private_chat" else {"kind":"none"}
    event=QQModule("qq",a).normalize_event(a.parse(raw(),now))
    assert e.ingest(event)
    assert e.process_one_event(now)
    actions=s.inspect("actions")
    assert len(actions)==2
    assert e.dispatch_one(now)
    assert len(w)==1
    assert e.dispatch_one(now+20)
    assert len(w)==2
    assert len([r for r in s.inspect("conversation_records") if r["role"]=="agent"])==2
    a.close()


def test_write_timeout_is_unknown_and_not_replayed(factory,now):
    e,s,a,w=setup(factory,timeout=True)
    from murasamebot.domain.models import Action
    action=Action("a",s.agent_id,"qq","456",None,"你好",now,now+100,kind="private_reply",recipient_id="123",visibility="private")
    assert a.send(action,{}).state=="unknown" and len(w)==1
    a.close()


def test_hard_wait_flushes_continuous_messages(factory,now):
    e,s,a,w=setup(factory); inbox=ChatBatches(s)
    for i in range(10):inbox.receive(a.parse(raw(i+1),now+i*19),s.binding("owner","qq"),now+i*19)
    assert inbox.flush(a.settings,"owner",lambda x:x,now+180)
    a.close()


def test_private_vision_is_reused_and_included_in_chat_payload(factory,now):
    from murasamebot.domain.models import VisualEvidence
    e,s,a,w=setup(factory);e.private_access["qq"]=a
    seen=[]
    class Vision:
        def enrich(self,document,at):
            seen.append(document)
            return replace(document,visual=VisualEvidence("complete",summary="图中是一朵花"))
    e.private_visions["qq"]=Vision()
    payloads=[]
    def decide(op,p):
        if op=="private_chat":
            payloads.append(p);return {"decision":"reply","text":"这朵花很好看。"}
        return {"kind":"none"}
    e.model.decide=decide
    event=QQModule("qq",a).normalize_event(a.parse(raw(message=[{"type":"image","data":{"url":"https://multimedia.nt.qq.com/test"}}]),now))
    e.ingest(event);e.process_one_event(now)
    assert seen[0].images and payloads[0]["private_images"]["visual"]["summary"]=="图中是一朵花"
    a.close()


def test_linked_owner_private_memory_is_shared_but_other_people_are_not(factory,now):
    from murasamebot.domain.models import Event
    e,s,a,w=setup(factory)
    s.bind("owner","bilibili","321")
    for eid,actor,text in (("owner-memory","321","主人喜欢绿色"),("other-memory","999","他人的私密内容")):
        event=Event(eid,"bilibili","robot",actor,eid,"private_message",text,now-50,"private")
        s.enqueue_event(event);s.finish_event(eid,"done")
    e.memory_service.shared_owner_private=True
    event=a.parse(raw(),now)
    ctx=e.memory_service.context(event,"owner",True,now)
    text=json.dumps(ctx,ensure_ascii=False)
    assert "主人喜欢绿色" in text and "他人的私密内容" not in text
    s.revoke_binding("owner","bilibili")
    assert "主人喜欢绿色" not in json.dumps(e.memory_service.context(event,"owner",True,now),ensure_ascii=False)
    a.close()


def test_unsigned_callback_is_rejected_without_ingestion(tmp_path):
    from io import BytesIO
    from test_architecture_and_config import write_config
    from murasamebot.bootstrap.config import Config
    from murasamebot.bootstrap.qq_receiver import handler_for
    path=write_config(tmp_path)
    cfg=Config(path)
    local={"platforms":{"qq":{"enabled":True}},"qq":{"callback_secret":"test"}}
    import yaml
    cfg.local_path.write_text(yaml.safe_dump(local),encoding="utf-8")
    h=object.__new__(handler_for(path));body=json.dumps(raw()).encode()
    h.path,h.command="/onebot/qq","POST"
    h.request_version,h.requestline="HTTP/1.1","POST /onebot/qq HTTP/1.1"
    h.headers={"Content-Length":str(len(body))};h.rfile=BytesIO(body);h.wfile=BytesIO()
    h.do_POST()
    assert b"401" in h.wfile.getvalue()


def test_signed_callback_commits_before_ack(factory,tmp_path,monkeypatch):
    import hashlib,hmac,yaml
    from io import BytesIO
    import murasamebot.bootstrap.qq_receiver as receiver
    from test_architecture_and_config import write_config
    from murasamebot.bootstrap.config import Config
    e,s,a,w=setup(factory)
    path=write_config(tmp_path)
    Config(path).local_path.write_text(yaml.safe_dump({"platforms":{"qq":{"enabled":True}},"qq":{"callback_secret":"test"}}),encoding="utf-8")
    monkeypatch.setattr(receiver,"prepare_store",lambda config:s)
    monkeypatch.setattr(receiver,"QQAdapter",lambda *args:a)
    h=object.__new__(receiver.handler_for(path));body=json.dumps(raw()).encode()
    h.path,h.command="/onebot/qq","POST"
    h.request_version,h.requestline="HTTP/1.1","POST /onebot/qq HTTP/1.1"
    h.headers={"Content-Length":str(len(body)),"X-Signature":"sha1="+hmac.new(b"test",body,hashlib.sha1).hexdigest()}
    h.rfile=BytesIO(body);h.wfile=BytesIO()
    h.do_POST()
    assert b"200 OK" in h.wfile.getvalue() and not w
    with s.connection() as db:assert db.execute("SELECT COUNT(*) FROM chat_inbox WHERE state='pending'").fetchone()[0]==1


def test_napcat_chunked_http_callback_commits_signed_original_bytes(factory,tmp_path,monkeypatch):
    import hashlib,hmac,yaml
    from http.server import ThreadingHTTPServer
    from threading import Thread
    import murasamebot.bootstrap.qq_receiver as receiver
    from test_architecture_and_config import write_config
    from murasamebot.bootstrap.config import Config
    e,s,a,w=setup(factory)
    path=write_config(tmp_path)
    Config(path).local_path.write_text(yaml.safe_dump({"platforms":{"qq":{"enabled":True}},"qq":{"callback_secret":"test"}}),encoding="utf-8")
    monkeypatch.setattr(receiver,"prepare_store",lambda config:s)
    monkeypatch.setattr(receiver,"QQAdapter",lambda *args:a)
    server=ThreadingHTTPServer(("127.0.0.1",0),receiver.handler_for(path))
    thread=Thread(target=server.serve_forever,daemon=True);thread.start()
    body=json.dumps(raw(),ensure_ascii=False).encode()
    try:
        with httpx.Client(trust_env=False) as client:
            response=client.post(f"http://127.0.0.1:{server.server_port}/onebot/qq",
                content=iter((body[:31],body[31:])),
                headers={"X-Signature":"sha1="+hmac.new(b"test",body,hashlib.sha1).hexdigest()})
        assert response.status_code==200 and response.json()=={} and not w
        with s.connection() as db:assert db.execute("SELECT COUNT(*) FROM chat_inbox WHERE state='pending'").fetchone()[0]==1
    finally:
        server.shutdown();server.server_close();thread.join()


@pytest.mark.parametrize("headers,body,status",[
    ({"Transfer-Encoding":"chunked"},b"40001\r\n",413),
    ({"Transfer-Encoding":"chunked"},b"1\r\nxNO",400),
    ({"Transfer-Encoding":"chunked","Content-Length":"1"},b"",400),
    ({"Transfer-Encoding":"chunked"},b"0\r\n\r\n",400),
    ({"Content-Length":"4"},b"xx",400),
    ({},b"{}",411),
])
def test_callback_rejects_oversize_or_invalid_http_framing(headers,body,status):
    from io import BytesIO
    from murasamebot.bootstrap.qq_receiver import read_body,BodyError
    with pytest.raises(BodyError) as exc:read_body(headers,BytesIO(body))
    assert exc.value.status==status


def test_qq_multilingual_private_reply_keeps_bilibili_language_rule(factory,now):
    from murasamebot.application.engine import checked_text
    from murasamebot.domain.models import PolicyError
    e,s,a,w=setup(factory);e.private_access["qq"]=a;e.sinks["qq"]=a
    e.private_policies["qq"]={"multilingual":True,"topic_filter":False}
    e.model.decide=lambda op,p: {"decision":"reply","text":"ムラサメ is here!"} if op=="private_chat" else {"kind":"none"}
    e.ingest(QQModule("qq",a).normalize_event(a.parse(raw(),now)))
    assert e.process_one_event(now) and e.dispatch_one(now) and len(w)==1
    with pytest.raises(PolicyError):checked_text("ムラサメ is here!",400)
    a.close()
