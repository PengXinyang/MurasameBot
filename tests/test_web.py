from http.server import ThreadingHTTPServer
from threading import Thread
from concurrent.futures import ThreadPoolExecutor

import httpx

from murasamebot.bootstrap.web import handler_for
from test_architecture_and_config import write_config


def test_dashboard_requires_token_and_rejects_foreign_origin(tmp_path):
    path = write_config(tmp_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(path, "local-token"))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        with httpx.Client(base_url=url, trust_env=False) as client:
            assert client.get("/").status_code == 200
            assert client.get("/api/config").status_code == 401
            headers = {"Authorization": "Bearer local-token"}
            response = client.get("/api/config", headers=headers)
            assert response.status_code == 200 and response.json()["bindings"][0]["external_id"] == "424242"
            assert response.json()["review"]["strategy"]=="inline"
            assert client.post("/api/config",json={"review":{"strategy":"independent"}},headers=headers).status_code==200
            assert client.get("/api/status",headers=headers).json()["review_strategy"]=="independent"
            assert client.post("/api/config",json={"review":{"strategy":"inline"}},headers=headers).status_code==200
            assert client.post("/api/config",json={"review":{"strategy":"unknown"}},headers=headers).status_code==400
            assert client.post("/api/config",json={"review":{"strategy":[]}},headers=headers).status_code==400
            paths = ["/api/status", "/api/data/tasks", "/api/data/actions", "/api/data/memories", "/api/data/audit"] * 3
            with ThreadPoolExecutor(max_workers=5) as pool:
                codes = list(pool.map(lambda p: client.get(p, headers=headers).status_code, paths))
            assert codes == [200] * len(paths)
            foreign = {**headers, "Origin": "https://attacker.invalid"}
            assert client.post("/api/resume", json={}, headers=foreign).status_code == 401
            assert client.post("/api/config", json={"browsing": {"interval_minutes": 120}}, headers=headers).status_code == 200
            assert client.get("/api/config", headers=headers).json()["browsing"]["interval_minutes"] == 120
            assert client.post("/api/config", json={"network": {"proxy_enabled": True, "proxy_url": "socks5://127.0.0.1:7890"},
                                                   "artwork": {"times": ["11:30"], "keywords": ["丛雨"], "enabled": True},
                                                   "bilibili": {"repost_owner_dynamics": False}}, headers=headers).status_code == 200
            updated = client.get("/api/config", headers=headers).json()
            assert updated["artwork"]["times"] == ["11:30"] and updated["network"]["proxy_url"].startswith("socks5:")
            assert not updated["bilibili"]["repost_owner_dynamics"]
            assert client.post("/api/config", json={"mode": "reviewed", "bilibili": {"private_messages_enabled": True,
                                                   "reply_poll_seconds": 15, "poll_seconds": 90}}, headers=headers).status_code == 200
            assert client.get("/api/config", headers=headers).json()["mode"] == "reviewed"
            assert client.post("/api/config", json={"engagement": {"video_coin": False}, "vision": {"max_images": 3, "enabled": True}}, headers=headers).status_code == 200
            current = client.get("/api/config", headers=headers).json()
            assert current["vision"]["max_images"] == 3 and not current["engagement"]["video_coin"]
            assert client.post("/api/config", json={"processing":{"model_concurrency":8},"model":{"daily_call_limit":500},
                "limits":{"daily_comments":100,"min_send_gap_seconds":5},"bilibili":{"inbox_batch_limit":90}},headers=headers).status_code == 200
            current = client.get("/api/config", headers=headers).json()
            assert current["processing"]["model_concurrency"] == 8 and current["model"]["daily_call_limit"] == 500
            assert current["limits"]["daily_comments"] == 100 and current["limits"]["min_send_gap_seconds"] == 5
            from murasamebot.bootstrap.config import Config
            from murasamebot.bootstrap.runtime import prepare_store
            prepare_store(Config(path)).set_setting("inbox_warnings",{"notifications":[{"source":"notifications","detail":"超过3页，保留90条"}]})
            status = client.get("/api/status",headers=headers).json()
            assert status["warnings"][0]["detail"] == "超过3页，保留90条"
            assert status["queues"]["pending_events"] == 0
            assert client.post("/api/config",json={"processing":{"model_concurrency":0}},headers=headers).status_code == 400
            assert client.post("/api/config",json={"expressions":{"enabled":True,"kaomoji_enabled":False,"max_per_message":1,"preferred_keywords":["千恋万花","丛雨"]},
                "browsing":{"comment_videos":False,"reply_comments":True,"recommend_owner":False}},headers=headers).status_code == 200
            current=client.get("/api/config",headers=headers).json()
            assert current["expressions"]["max_per_message"] == 1 and current["browsing"]["reply_comments"]
            assert not current["expressions"]["kaomoji_enabled"] and current["expressions"]["preferred_keywords"][0]=="千恋万花"
            assert not current["browsing"]["comment_videos"] and not current["browsing"]["recommend_owner"]
            assert client.post("/api/config",json={"expressions":{"max_per_message":3}},headers=headers).status_code == 400
            assert client.post("/api/config",json={"expressions":{"kaomoji_enabled":"true"}},headers=headers).status_code == 400
            assert client.post("/api/config", json={"engagement": {"coin_amount": 2}}, headers=headers).status_code == 400
            assert client.post("/api/config", json={"bilibili": {"private_mutual_only": False}}, headers=headers).status_code == 400
            assert client.post("/api/config", json={"artwork": {"permissions": []}}, headers=headers).status_code == 400
            assert client.post("/api/config", json={"model": {"api_key": "bad"}}, headers=headers).status_code == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_dashboard_can_refresh_readonly_emoji_catalog(tmp_path,monkeypatch):
    from types import SimpleNamespace
    import murasamebot.bootstrap.web as web
    from murasamebot.application.expressions import ExpressionSettings
    from murasamebot.domain.models import Emoji
    path=write_config(tmp_path)
    token=Emoji("bilibili:102084","[千恋万花表情包_丛雨Ciallo]","丛雨Ciallo")
    calls=[]
    platform=SimpleNamespace(account=lambda:{"uid":"999"},emoji_options=lambda uid,channel:calls.append((uid,channel)) or (token,))
    runtime=SimpleNamespace(platform=platform,engine=SimpleNamespace(expressions=SimpleNamespace(settings=ExpressionSettings())),close=lambda:None)
    monkeypatch.setattr(web,"build",lambda cfg:runtime)
    server=ThreadingHTTPServer(("127.0.0.1",0),handler_for(path,"token"))
    thread=Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}",trust_env=False) as client:
            assert client.post("/api/emojis/refresh",json={}).status_code==401
            response=client.post("/api/emojis/refresh",json={},headers={"Authorization":"Bearer token"})
            assert response.status_code==200 and response.json()["preferred"]==[token.token]
            assert calls==[("999","public")]
    finally:
        server.shutdown();server.server_close();thread.join(timeout=3)


def test_dashboard_assets_do_not_expose_character_configuration(tmp_path):
    path=write_config(tmp_path)
    server=ThreadingHTTPServer(("127.0.0.1",0),handler_for(path,"token"))
    thread=Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}",trust_env=False) as client:
            css=client.get("/assets/dashboard.css")
            assert css.status_code==200 and css.headers["content-type"].startswith("text/css")
            assert "hero-art" in css.text
            for name in ("normal","happy"):
                image=client.get(f"/assets/character/{name}.png")
                assert image.status_code==200 and image.content.startswith(b"\x89PNG\r\n\x1a\n")
                assert image.headers["content-type"]=="image/png"
                assert image.headers["x-content-type-options"]=="nosniff"
            for name, mime, magic in [("dashboard-smile.jpg", "image/jpeg", b"\xff\xd8\xff"),
                                      ("dashboard-letter.jpg", "image/jpeg", b"\xff\xd8\xff"),
                                      ("dashboard-kimono.png", "image/png", b"\x89PNG\r\n\x1a\n")]:
                image=client.get('/assets/character/'+name)
                assert image.status_code==200 and image.content.startswith(magic)
                assert image.headers["content-type"]==mime
                assert image.headers["x-content-type-options"]=="nosniff"
            for name in ("settings.yml",".env","../settings.yml","%2e%2e%2fsettings.yml","%5c..%5c.env"):
                response=client.get("/assets/character/"+name,headers={"Authorization":"Bearer token"})
                assert response.status_code==404
            assert client.get("/assets/character/normal.png",headers={"Host":"untrusted.invalid"}).status_code==401
            assert client.get("/api/config").status_code==401
    finally:
        server.shutdown();server.server_close();thread.join(timeout=3)


def test_missing_optional_dashboard_art_does_not_break_status(tmp_path):
    from pathlib import Path
    import yaml
    path=write_config(tmp_path)
    data=yaml.safe_load(path.read_text(encoding="utf-8"))
    source=tmp_path/"without-art"/"settings.yml"
    source.parent.mkdir()
    source.write_bytes(Path(data["character_source"]).read_bytes())
    data["character_source"]=str(source)
    path.write_text(yaml.safe_dump(data,allow_unicode=True),encoding="utf-8")
    server=ThreadingHTTPServer(("127.0.0.1",0),handler_for(path,"token"))
    thread=Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}",trust_env=False) as client:
            assert client.get("/").status_code==200
            assert client.get("/assets/character/happy.png").status_code==404
            assert client.get("/assets/dashboard.css").status_code==200
            assert client.get("/api/status",headers={"Authorization":"Bearer token"}).status_code==200
    finally:
        server.shutdown();server.server_close();thread.join(timeout=3)
