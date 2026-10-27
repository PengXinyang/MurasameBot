from http.server import ThreadingHTTPServer
from threading import Thread

import httpx

from murasamebot.bootstrap.config import Config
from murasamebot.bootstrap.runtime import prepare_store
from murasamebot.bootstrap.web import handler_for
from test_architecture_and_config import write_config
from test_memory import archive


def test_memory_console_search_sources_correction_forget_and_configuration(tmp_path, now):
    path=write_config(tmp_path)
    store=prepare_store(Config(path))
    archive(store,now,count=3,text="我喜欢摄影")
    records=store.memory_records("unlinked:wechat:peer","conversation:wechat:room","private_dialogue")
    assert store.memory_commit(records,[{"kind":"fact","text":"用户喜欢摄影。","sources":[records[0]["record_id"]]}],"summary",0,now)
    product=store.inspect("memory_products")[0]
    server=ThreadingHTTPServer(("127.0.0.1",0),handler_for(path,"token"))
    thread=Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}",trust_env=False) as client:
            assert client.get("/api/memory/scopes").status_code==401
            headers={"Authorization":"Bearer token"}
            def post(route,payload):
                response=client.post('/api/'+route,json=payload,headers=headers)
                assert response.status_code==200,response.text
                return response.json()
            scopes=client.get("/api/memory/scopes",headers=headers).json()
            scope={k:scopes[0][k] for k in ("person_id","scope","purpose")}
            assert len(post("memory/search",{**scope,"query":"摄影"}))==3
            assert not post("memory/search",{**scope,"purpose":"public_dialogue","query":"摄影"})
            assert post("memory/sources",{"product_id":product["product_id"]})[0]["text"]=="我喜欢摄影"
            post("memory/edit",{"id":product["product_id"],"text":"更正为喜欢料理。"})
            corrected=next(p for p in store.inspect("memory_products") if p["active"])
            post("memory/edit",{"id":corrected["product_id"],"pinned":True})
            post("config",{"memory":{"daily_call_limit":0,"recent_messages":25,"review_time":"22:30"}})
            config=client.get("/api/config",headers=headers).json()
            assert config["memory"]["daily_call_limit"]==0 and config["memory"]["recent_messages"]==25
            assert client.post("/api/config",json={"memory":{"daily_call_limit":-1}},headers=headers).status_code==400
            assert client.post("/api/config",json={"memory":{"private_to_public":True}},headers=headers).status_code==400
            post("memory/rebuild",scope)
            assert next(p for p in store.inspect("memory_products") if p["product_id"]==corrected["product_id"])["active"]
            post("memory/edit",{"id":corrected["product_id"],"forget":True})
            assert not any(p["active"] for p in store.inspect("memory_products"))
            assert len(post("memory/search",{**scope,"query":"摄影"}))==2
            post("memory/retry",{})
            assert client.get("/api/status",headers=headers).json()["memory"]["conversation_records"]==2
    finally:
        server.shutdown();server.server_close();thread.join(timeout=3)
