import struct

import httpx
import pytest

from murasamebot.adapters.platforms.bilibili_frames import VideoShotReader, fetch_preview_index
from murasamebot.domain.models import PlatformError
from test_video_frames import shot


VIDEO = {"aid": 42, "bvid": "BVexample", "pages": [{"cid": 1, "duration": 80}]}


def test_empty_json_index_recovers_binary_without_more_api_calls():
    calls = []
    def request(path, *, params):
        calls.append(path)
        return {**shot(), "index": [], "pvdata": "https://i0.hdslb.com/a.bin"}, None
    reader = VideoShotReader(request, index_reader=lambda url: list(range(0, 80, 10)))
    images, context = reader.read(VIDEO)
    assert len(images) == 6 and context.status == "complete"
    assert calls == ["/x/player/videoshot"]
    assert images[0].crop == (16, 0, 16, 16)


def test_av_identifier_fallback_keeps_cid_and_index_flag():
    calls = []
    def request(path, *, params):
        calls.append((path, params))
        return ({**shot(), "index": []} if "bvid" in params else shot()), None
    images, context = VideoShotReader(request).read(VIDEO)
    assert len(images) == 6 and context.status == "complete"
    assert calls[-1] == ("/x/player/videoshot", {"aid": "42", "cid": "1", "index": 1})


def test_pvideo_is_only_used_for_first_part_and_failure_cache_is_bounded():
    calls = []
    def request(path, *, params):
        calls.append((path, params))
        return (shot() if path == "/pvideo" else {**shot(), "index": []}), None
    reader = VideoShotReader(request, count=4)
    video = {**VIDEO, "pages": [{"cid": 1}, {"cid": 2}]}
    images, context = reader.read(video)
    assert len(images) == 2 and context.sampled_parts == (1,)
    assert sum(path == "/pvideo" for path, _ in calls) == 1
    assert len(calls) == 5
    reader.read(video)
    assert len(calls) == 5
    assert "时间表缺失" in "".join(context.limitations)


@pytest.mark.parametrize("stage", ["first", "alternate", "binary"])
@pytest.mark.parametrize("error", [PlatformError("limited", code="-509"), PlatformError("auth", pause=True),
                                  PlatformError("quiet", code="read_deferred", retry_at=2000)])
def test_fallbacks_never_swallow_limits_or_auth(stage, error):
    calls = []
    def request(path, *, params):
        calls.append(path)
        if stage == "first" or stage == "alternate" and len(calls) == 2:
            raise error
        return {**shot(), "index": [], "pvdata": "https://i0.hdslb.com/a.bin"}, None
    def binary(url):
        raise error
    with pytest.raises(PlatformError):
        VideoShotReader(request, index_reader=binary if stage == "binary" else None).read(VIDEO)
    assert len(calls) == (2 if stage == "alternate" else 1)


def test_binary_index_is_big_endian_and_sends_no_cookie_or_auth():
    def handle(request):
        assert "cookie" not in request.headers and "authorization" not in request.headers
        return httpx.Response(200, content=struct.pack(">5H", 0, 0, 5, 8, 300))
    client = httpx.Client(transport=httpx.MockTransport(handle), cookies={"secret": "cookie"}, headers={"Authorization": "private"})
    for host in ("i0.hdslb.com", "boss.hdslb.com"):
        assert fetch_preview_index(f"//{host}/a.bin", client=client) == [0, 5, 8, 300]


@pytest.mark.parametrize("data", [b"", b"abc", b"x"*20004, struct.pack(">3H", 1, 5, 8), struct.pack(">4H", 0, 5, 3, 8)], ids=["empty","odd","oversize","header","unsorted"])
def test_invalid_binary_does_not_invent_times(data):
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=data)))
    assert fetch_preview_index("https://i0.hdslb.com/a.bin", client=client) == []


@pytest.mark.parametrize("url", ["https://evil.invalid/a", "https://i0.hdslb.com.evil.invalid/a", "https://user@i0.hdslb.com/a",
                                 "https://i0.hdslb.com:123/a", "file:///a"])
def test_binary_rejects_untrusted_urls_without_request(url):
    def unexpected(request):
        pytest.fail("untrusted URL requested")
    assert fetch_preview_index(url, client=httpx.Client(transport=httpx.MockTransport(unexpected))) == []


@pytest.mark.parametrize("status", [302, 403, 412, 429])
def test_binary_redirects_and_risk_are_not_bypassed(status):
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(status, headers={"location": "https://evil.invalid/a"})
    client = httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=True)
    if status == 302:
        assert fetch_preview_index("https://i0.hdslb.com/a.bin", client=client) == []
    else:
        with pytest.raises(PlatformError):
            fetch_preview_index("https://i0.hdslb.com/a.bin", client=client)
    assert len(calls) == 1
