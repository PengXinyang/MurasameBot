import json

import httpx
import pytest

from murasamebot.adapters.platforms.bilibili import BilibiliAdapter
from murasamebot.domain.models import PlatformError
from test_bilibili_contracts import credentials
from test_engagement import reaction


def bot(tmp_path, *, device=None, code=0, existing="", timeout=False):
    calls = []
    path = credentials(tmp_path)
    if existing:
        stored = json.loads(path.read_text())
        stored["buvid3"] = existing
        path.write_text(json.dumps(stored))
    original = path.read_bytes()

    def handle(request):
        calls.append(request)
        if request.url.path.endswith("/nav"):
            data = {"isLogin": True, "mid": 999, "money": 5}
        elif request.url.path.endswith("/coins"):
            data = {"multiply": 0}
        elif request.url.path.endswith("/finger/spi"):
            return httpx.Response(200, json={"code": 0, "data": device or {}})
        else:
            assert request.method == "POST"
            if timeout:
                raise httpx.ReadTimeout("unknown")
            return httpx.Response(200, json={"code": code, "message": "private upstream text"})
        return httpx.Response(200, json={"code": 0, "data": data})

    return BilibiliAdapter(path, client=httpx.Client(transport=httpx.MockTransport(handle))), calls, original


def test_legacy_auth_obtains_real_device_once_without_overwriting_login(tmp_path):
    adapter, calls, original = bot(tmp_path, device={"b_3": "server-device-three", "b_4": "server-device-four=="})
    for _ in range(2):
        assert adapter.send(reaction(), {}).state == "accepted"
    # Sender jobs rebuild their runtime; reuse the session-bound device across
    # a fresh adapter as well, without modifying the actual auth file.
    rebuilt = BilibiliAdapter(adapter.credentials_path, client=adapter.client)
    assert rebuilt.send(reaction(), {}).state == "accepted"
    assert sum(r.url.path.endswith("/finger/spi") for r in calls) == 1
    posts = [r for r in calls if r.method == "POST"]
    assert len(posts) == 3
    assert all("buvid3=server-device-three" in r.headers["cookie"] for r in posts)
    assert adapter.credentials_path.read_bytes() == original
    saved = adapter.credentials_path.with_suffix(".device.json").read_text()
    assert "server-device-three" in saved and "test-secret" not in saved and '"csrf"' not in saved


@pytest.mark.parametrize("device", [{}, {"b_3": 123}, {"b_3": "bad;cookie"}, {"b_3": "\nsecret"}])
def test_missing_invalid_device_does_not_write_or_fabricate_one(tmp_path, device):
    adapter, calls, original = bot(tmp_path, device=device)
    with pytest.raises(PlatformError) as error:
        adapter.send(reaction(), {})
    assert error.value.code == "device_cookie_missing" and error.value.pause
    assert not any(r.method == "POST" for r in calls)
    assert adapter.credentials_path.read_bytes() == original


@pytest.mark.parametrize("code,reason,pause", [(-401, "不是硬币不足", True), (-104, "硬币余额不足", False),
                                              (-111, "CSRF", True), (34004, "投币间隔过短", False)])
def test_platform_reject_is_explained_without_replay_or_rotating_device(tmp_path, code, reason, pause):
    adapter, calls, original = bot(tmp_path, code=code, existing="existing-server-device")
    with pytest.raises(PlatformError, match=reason) as error:
        adapter.send(reaction(), {})
    assert error.value.code == str(code) and error.value.pause == pause
    assert len([r for r in calls if r.method == "POST"]) == 1
    assert not any(r.url.path.endswith("/finger/spi") for r in calls)
    assert "private upstream text" not in str(error.value)
    assert adapter.credentials_path.read_bytes() == original


def test_device_read_cannot_bypass_final_gate_or_repeat_ambiguous_write(tmp_path):
    adapter, calls, _ = bot(tmp_path, device={"b_3": "server-device"}, timeout=True)
    assert adapter.send(reaction(), {}, before_write=lambda: False).state == "failed"
    assert not any(r.method == "POST" for r in calls)
    assert adapter.send(reaction(), {}).state == "unknown"
    assert sum(r.method == "POST" for r in calls) == 1


def test_damaged_device_cache_is_not_rotated_or_written_through(tmp_path):
    adapter, calls, original = bot(tmp_path, device={"b_3": "server-device"})
    adapter.credentials_path.with_suffix(".device.json").write_text("invalid-json")
    with pytest.raises(PlatformError, match="设备缓存无效"):
        adapter.send(reaction(), {})
    assert not any(r.method == "POST" or r.url.path.endswith("/finger/spi") for r in calls)
    assert adapter.credentials_path.read_bytes() == original


def test_device_cache_from_another_login_session_is_not_reused(tmp_path):
    adapter, calls, original = bot(tmp_path, device={"b_3": "new-session-device"})
    adapter.credentials_path.with_suffix(".device.json").write_text(json.dumps({
        "session_signature": "different-session", "buvid3": "other-session-device"}))
    assert adapter.send(reaction(), {}).state == "accepted"
    post = next(r for r in calls if r.method == "POST")
    assert "buvid3=new-session-device" in post.headers["cookie"]
    assert "other-session-device" not in post.headers["cookie"]
    assert adapter.credentials_path.read_bytes() == original
