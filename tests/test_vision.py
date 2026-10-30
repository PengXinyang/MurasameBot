from dataclasses import replace
from io import BytesIO
import json

import httpx
from PIL import Image
import pytest

from murasamebot.adapters.media.http_images import HTTPImageReader
from murasamebot.adapters.models.chat_completions import ChatCompletionsModel
from murasamebot.adapters.platforms.bilibili import BilibiliAdapter
from murasamebot.application.vision import VisionService, VisionSettings
from murasamebot.domain.models import ContentRef, Document, ImageRef, MediaError, ModelError
from test_bilibili_contracts import credentials


def png():
    buffer = BytesIO()
    Image.new("RGB", (16, 16), "red").save(buffer, format="PNG")
    return buffer.getvalue()


def reader(handler=None):
    return HTTPImageReader(hosts=("i0.hdslb.com",), client=httpx.Client(transport=httpx.MockTransport(
        handler or (lambda r: httpx.Response(200, content=png())))))


def test_image_reader_streams_actual_pixels_without_cookies():
    calls = []
    def handle(request):
        assert "cookie" not in request.headers
        calls.append(request)
        return httpx.Response(200, content=png(), headers={"Set-Cookie": "bad=token; Path=/"})
    client = reader(handle)
    ref = ImageRef("https://i0.hdslb.com/picture.png", 1)
    for _ in range(2):
        result = client.load(ref, 4096)
        assert result.data_url.startswith("data:image/png;base64,") and result.size_bytes == len(png())
    assert len(calls) == 2


@pytest.mark.parametrize("url", ["http://i0.hdslb.com/a", "https://evil.invalid/a", "https://i0.hdslb.com.evil.invalid/a", "https://user@i0.hdslb.com/a", "https://i0.hdslb.com:123/a"])
def test_image_reader_rejects_untrusted_urls(url):
    with pytest.raises(MediaError):
        reader().load(ImageRef(url, 1), 4096)


@pytest.mark.parametrize("response", [httpx.Response(302, headers={"Location": "https://evil.invalid/a"}), httpx.Response(200, content=b"not an image"), httpx.Response(200, content=png())])
def test_redirect_invalid_image_and_size_limit_fail_closed(response):
    with pytest.raises(MediaError):
        reader(lambda r: response).load(ImageRef("https://i0.hdslb.com/a", 1), 8)


class VisionModel:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def decide(self, operation, payload):
        self.calls.append((operation, payload))
        if self.fail:
            raise ModelError("failed")
        return {"summary": "红色图块", "images": [{"index": i, "description": "纯红色图块"} for i in payload["image_indices"]], "uncertainties": []}


def document(count=1):
    return Document(ContentRef("bilibili", "post", "900"), "动态", "看看图片", "text", images=tuple(ImageRef(f"https://i0.hdslb.com/{i}.png", i) for i in range(1, count + 1)))


def test_vision_persists_evidence_and_reuses_cache(factory, now):
    _, store, _, _ = factory()
    model = VisionModel()
    vision = VisionService(VisionSettings(), reader(), model, store, model_name="vision")
    doc = vision.enrich(document(), now)
    assert doc.evidence_level == "text_visual" and doc.visual.status == "complete"
    store.cache_document(doc, now)
    cached = store.cached_document(doc.ref, now)
    assert cached == doc and "base64" not in json.dumps(doc.public_context())
    assert vision.enrich(document(), now + 30, previous=cached).visual == doc.visual
    assert len(model.calls) == 1
    changed = replace(document(), text="正文改变了")
    vision.enrich(changed, now + 40, previous=cached)
    assert len(model.calls) == 2


def test_partial_coverage_preserves_original_indices(factory, now):
    _, store, _, _ = factory()
    def handle(request):
        return httpx.Response(404) if request.url.path == "/1.png" else httpx.Response(200, content=png())
    model = VisionModel()
    vision = VisionService(VisionSettings(max_images=2), reader(handle), model, store, model_name="vision")
    doc = vision.enrich(document(3), now)
    assert doc.visual.status == "partial" and doc.visual.indices == (2,) and doc.visual.total_images == 3
    assert model.calls[0][1]["image_indices"] == [2]


def test_failed_vision_never_becomes_visual_evidence_and_has_retry_backoff(factory, now):
    _, store, _, _ = factory()
    model = VisionModel(fail=True)
    vision = VisionService(VisionSettings(), reader(), model, store, model_name="vision")
    doc = vision.enrich(replace(document(), text=""), now)
    assert doc.evidence_level == "metadata" and doc.visual.status == "failed" and not doc.visual.indices
    detail = next(r["detail"] for r in store.inspect("audit") if r["kind"] == "vision_result")
    assert "图片下载1/1张" in detail and "有效识别0/1张" in detail
    assert "模型调用或JSON解析" in detail
    vision.enrich(replace(document(), text=""), now + 1, previous=doc)
    assert len(model.calls) == 1
    vision.enrich(replace(document(), text=""), now + 601, previous=doc)
    assert len(model.calls) == 2


def test_disabling_vision_removes_prior_visual_evidence(factory, now):
    _, store, _, _ = factory()
    model = VisionModel()
    enabled = VisionService(VisionSettings(), reader(), model, store, model_name="vision")
    doc = enabled.enrich(document(), now)
    disabled = VisionService(VisionSettings(enabled=False), reader(), model, store, model_name="vision")
    result = disabled.enrich(doc, now, previous=doc)
    assert result.evidence_level == "text" and result.visual.status == "disabled" and not result.visual.summary


def test_public_reply_receives_actual_visual_summary_and_parent(factory, now):
    from murasamebot.domain.models import Event
    engine, store, model, platform = factory()
    doc = replace(document(), parent=ContentRef("bilibili", "post", "900"))
    platform.documents[doc.ref.key] = doc
    store.cache_document(replace(doc, images=(), text="旧正文"), now - 60)
    engine.vision = VisionService(VisionSettings(), reader(), VisionModel(), store, model_name="vision")
    engine.ingest(Event("visual-at", "bilibili", "robot", "123", "public-post", "post_mention", "这张图怎样", now, content=doc.ref, share_content=doc.parent))
    assert engine.process_one_event(now)
    content = next(payload["content"] for op, payload in model.calls if op == "reply")
    assert content["visual"]["summary"].startswith("红色图块") and content["visual"]["indices"] == (1,)
    assert "base64" not in json.dumps(content)


def test_gateway_sends_images_as_actual_multimodal_blocks(factory):
    _, store, _, _ = factory()
    image = reader().load(document().images[0], 4096)
    def handle(request):
        body = json.loads(request.content)
        blocks = body["messages"][-1]["content"]
        assert isinstance(blocks, list) and "base64" not in blocks[0]["text"]
        assert blocks[-1] == {"type": "image_url", "image_url": {"url": image.data_url, "detail": "auto"}}
        assert "max_tokens" not in body
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"summary":"红色"}'}, "finish_reason": "stop"}]})
    gateway = ChatCompletionsModel(base_url="https://model.invalid", name="vision", api_key="secret", store=store,
                                  timezone="Asia/Hong_Kong", daily_limit=10, client=httpx.Client(transport=httpx.MockTransport(handle)))
    gateway.decide("describe_images", {"image_indices": [1], "_image_inputs": [{"index": 1, "data_url": image.data_url, "detail": "auto"}]})


def test_dynamic_draw_and_opus_images_deduplicate_and_include_forwarded_images():
    item = {"modules": {"module_dynamic": {"major": {"draw": {"items": [{"src": "//i0.hdslb.com/1.png"}]},
        "opus": {"pics": [{"url": "https://i0.hdslb.com/1.png"}, {"url": "https://i0.hdslb.com/2.png"}]}}}},
        "orig": {"modules": {"module_dynamic": {"major": {"draw": {"items": [{"src": "https://i0.hdslb.com/3.png"}]}}}}}}
    images = BilibiliAdapter.dynamic_images(item)
    assert len(images) == 3 and [image.index for image in images] == [1, 2, 3]


def test_video_subtitles_can_be_disabled_without_player_or_video_download(tmp_path):
    paths = []
    def handle(request):
        paths.append(request.url.path)
        data = {"title": "摄影入门", "desc": "介绍曝光", "bvid": "BVtest", "pages": [{"cid": 123}], "owner": {"mid": 123}} if request.url.path.endswith("/view") else {"replies": []}
        return httpx.Response(200, json={"code": 0, "data": data})
    adapter = BilibiliAdapter(credentials(tmp_path), read_video_subtitles=False, client=httpx.Client(transport=httpx.MockTransport(handle)))
    doc = adapter.read(ContentRef("bilibili", "video", "42"))
    assert doc.title == "摄影入门" and doc.text == "介绍曝光" and doc.evidence_level == "metadata"
    assert paths == ["/x/web-interface/view", "/x/v2/reply/main"]


@pytest.mark.parametrize("subtitles", ["实际取得的字幕内容", "", "error"])
def test_available_subtitles_are_combined_with_description_and_title(tmp_path, subtitles):
    from murasamebot.domain.models import PlatformError
    calls = []
    def handle(request):
        data = {"title": "视频标题", "desc": "完整视频简介", "bvid": "BVtest", "pages": [{"cid": 123}], "owner": {"mid": 123}} if request.url.path.endswith("/view") else {"replies": []}
        return httpx.Response(200, json={"code": 0, "data": data})
    adapter = BilibiliAdapter(credentials(tmp_path), client=httpx.Client(transport=httpx.MockTransport(handle)))
    def subtitle(bvid, cid):
        calls.append((bvid, cid))
        if subtitles == "error":
            raise PlatformError("普通字幕接口不可用")
        return subtitles
    adapter._subtitle = subtitle
    doc = adapter.read(ContentRef("bilibili", "video", "42"))
    assert calls == [("BVtest", "123")]
    assert doc.title == "视频标题" and "完整视频简介" in doc.text
    if subtitles and subtitles != "error":
        assert "实际取得的字幕内容" in doc.text and doc.evidence_level == "transcript"
    else:
        assert doc.text == "完整视频简介" and doc.evidence_level == "metadata"
