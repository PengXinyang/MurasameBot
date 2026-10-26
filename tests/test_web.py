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
            assert client.post("/api/config", json={"artwork": {"permissions": []}}, headers=headers).status_code == 400
            assert client.post("/api/config", json={"model": {"api_key": "bad"}}, headers=headers).status_code == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
