from dataclasses import asdict, replace
import base64
from io import BytesIO
import json
from pathlib import Path

import httpx
from PIL import Image
import pytest
import yaml

from murasamebot.adapters.media.http_images import HTTPImageReader
from murasamebot.adapters.models.chat_completions import ChatCompletionsModel
from murasamebot.adapters.platforms.bilibili import BilibiliAdapter
from murasamebot.adapters.platforms.bilibili_frames import VideoShotReader, sample_shot
from murasamebot.application.vision import VisionService, VisionSettings
from murasamebot.bootstrap.config import Config
from murasamebot.domain.models import ContentRef, Document, Event, FrameContext, ImageRef, MediaError, PlatformError
from murasamebot.domain.topics import document_topic
from test_bilibili_contracts import credentials
from test_vision import VisionModel


REF = ContentRef("bilibili", "video", "42")


def shot():
    # Two 2x2 sheets, eight real frames, and the documented zero header.
    return {"img_x_len": 2, "img_y_len": 2, "img_x_size": 16, "img_y_size": 16,
            "image": ["//i0.hdslb.com/sprite0.png", "//i0.hdslb.com/sprite1.png"],
            "index": [0, 0, 10, 20, 30, 40, 50, 60, 70]}


def sprite():
    image = Image.new("RGB", (32, 32), "red")
    image.paste("blue", (16, 16, 32, 32))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def image_reader(calls=None):
    def handle(request):
        assert "cookie" not in request.headers
        if calls is not None:
            calls.append(str(request.url))
        return httpx.Response(200, content=sprite())
    return HTTPImageReader(hosts=("i0.hdslb.com",), client=httpx.Client(transport=httpx.MockTransport(handle)))


def video_document(now, level="transcript"):
    return Document(REF, "角色舞蹈", "简介与已取得字幕", level, author_id="123",
                    images=(ImageRef("https://i0.hdslb.com/sprite0.png", 1, (0, 0, 16, 16), 10, 1),
                            ImageRef("https://i0.hdslb.com/sprite0.png", 2, (16, 16, 16, 16), 70, 1)),
                    frame_context=FrameContext("complete", 2, 3, 1, (1,), ("仅采样截图，未完整观看",), now, now + 600))


def test_header_and_second_sheet_use_local_cell_coordinates():
    frames = sample_shot(shot(), 8, 80, 2)
    assert [frame.timestamp_seconds for frame in frames] == list(range(0, 80, 10))
    assert frames[4].url.endswith("sprite1.png") and frames[4].crop == (0, 0, 16, 16)
    assert frames[-1].crop == (16, 16, 16, 16) and frames[-1].part == 2
    data = shot()
    data["index"] = data["index"][1:]
    assert sample_shot(data, 8, 80, 2) == frames


def test_sampling_spreads_over_duration_and_removes_duplicate_timestamps():
    data = shot()
    data["index"] = [0, 0, 0, 10, 10, 40, 50, 60, 70]
    frames = sample_shot(data, 3, 80, 1)
    assert len(frames) == 3 and len({f.timestamp_seconds for f in frames}) == 3
    assert frames[0].timestamp_seconds <= 20 and frames[-1].timestamp_seconds >= 60


@pytest.mark.parametrize("patch", [{"img_x_len": 0}, {"img_y_size": True}, {"img_x_size": 99999},
                                   {"image": []}, {"index": None}, {"image": ["https://evil.invalid/a"]},
                                   {"image": ["https://user@i0.hdslb.com/a"]}])
def test_invalid_snapshot_data_does_not_create_frames(patch):
    assert sample_shot({**shot(), **patch}, 6, 80, 1) == ()


def test_multiple_parts_share_frame_budget_and_never_use_part_number_as_index():
    calls, clock = [], [1000]
    def request(path, *, params):
        assert path == "/x/player/videoshot" and params["index"] == 1
        calls.append(params["cid"])
        return shot(), None
    sampler = VideoShotReader(request, count=6, max_parts=3, clock=lambda: clock[0])
    data = {"bvid": "BVexample", "pages": [{"cid": i, "duration": 80} for i in range(1, 5)]}
    frames, coverage = sampler.read(data)
    assert len(frames) == 6 and [f.part for f in frames] == [1, 1, 2, 2, 3, 3]
    assert [f.index for f in frames] == [1, 2, 3, 4, 5, 6]
    assert calls == ["1", "2", "3"] and coverage.total_parts == 4
    assert coverage.status == "partial" and coverage.sampled_parts == (1, 2, 3)
    assert sampler.read(data) == (frames, coverage) and len(calls) == 3
    clock[0] += 601
    assert sampler.read(data) == (frames, coverage) and len(calls) == 3


def test_missing_snapshot_is_cached_then_retried_without_losing_other_parts():
    now, calls = [1000], []
    def request(path, *, params):
        calls.append(params["cid"])
        if params["cid"] == "1":
            raise PlatformError("missing", code="40003")
        return shot(), None
    sampler = VideoShotReader(request, count=4, clock=lambda: now[0])
    data = {"bvid": "BVexample", "pages": [{"cid": 1}, {"cid": 2}]}
    images, context = sampler.read(data)
    assert len(images) == 2 and context.status == "partial" and context.sampled_parts == (2,)
    sampler.read(data)
    assert len(calls) == 2
    now[0] += 601
    sampler.read(data)
    assert len(calls) == 4


@pytest.mark.parametrize("error", [PlatformError("auth", pause=True), PlatformError("limited", code="-509"),
                                  PlatformError("quiet", code="read_deferred", retry_at=2000)])
def test_snapshot_does_not_swallow_auth_or_real_read_backoff(error):
    def request(*args, **kwargs):
        raise error
    with pytest.raises(PlatformError):
        VideoShotReader(request).read({"bvid": "BVexample", "pages": [{"cid": 1}]})


def test_crop_uses_actual_pixels_and_downloads_shared_sprite_once_per_batch():
    calls = []
    reader = image_reader(calls)
    with reader.batch(10000):
        red = reader.load(ImageRef("https://i0.hdslb.com/a", 1, (0, 0, 16, 16)), 4096)
        blue = reader.load(ImageRef("https://i0.hdslb.com/a", 2, (16, 16, 16, 16)), 4096)
    assert len(calls) == 1 and (blue.width, blue.height) == (16, 16)
    assert red.sha256 != blue.sha256
    with Image.open(BytesIO(base64.b64decode(blue.data_url.split(",")[1]))) as cropped:
        r, g, b = cropped.getpixel((8, 8))
        assert b > 220 and r < 20 and g < 20
    with reader.batch(10000):
        reader.load(ImageRef("https://i0.hdslb.com/a", 1), 4096)
    assert len(calls) == 2  # no image bytes retained between decisions


@pytest.mark.parametrize("crop", [(17, 17, 16, 16), (-1, 0, 16, 16), (0, 0, 0, 16), (0, 0, 1.5, 16)])
def test_last_partial_sheet_and_invalid_crop_fail_closed(crop):
    with pytest.raises(MediaError):
        image_reader().load(ImageRef("https://i0.hdslb.com/a", 1, crop), 4096)


def test_source_downloads_share_a_hard_batch_byte_budget():
    reader = image_reader()
    with reader.batch(len(sprite()) + 1):
        reader.load(ImageRef("https://i0.hdslb.com/a", 1), 4096)
        with pytest.raises(MediaError):
            reader.load(ImageRef("https://i0.hdslb.com/b", 2), 4096)


def test_vision_receives_real_crops_with_times_and_preserves_transcript(factory, now):
    _, store, _, _ = factory()
    model, calls = VisionModel(), []
    service = VisionService(VisionSettings(video_frame_count=2), image_reader(calls), model, store, model_name="vision")
    doc = service.enrich(video_document(now), now)
    assert doc.evidence_level == "transcript_visual" and doc.visual.status == "complete"
    payload = model.calls[0][1]
    assert payload["media_kind"] == "video_frames" and len(calls) == 1
    assert payload["frame_samples"] == [{"index": 1, "part": 1, "timestamp_seconds": 10, "width": 16, "height": 16},
                                         {"index": 2, "part": 1, "timestamp_seconds": 70, "width": 16, "height": 16}]
    assert "70秒" in doc.visual.summary and "未完整观看" in doc.visual.limitations[0]
    store.cache_document(doc, now)
    restored = store.cached_document(REF, now)
    assert restored == doc and "base64" not in json.dumps(asdict(restored))
    assert service.enrich(video_document(now), now + 1, previous=restored).visual == doc.visual
    assert len(model.calls) == 1
    changed = replace(video_document(now), images=(replace(doc.images[0], crop=(16, 16, 16, 16)), doc.images[1]))
    service.enrich(changed, now + 2, previous=restored)
    assert len(model.calls) == 2  # same sprite URL but a different crop is new evidence


@pytest.mark.parametrize("settings", [VisionSettings(enabled=False), VisionSettings(video_frames_enabled=False)])
def test_disabling_frames_clears_old_visual_evidence_but_keeps_subtitles(factory, now, settings):
    _, store, _, _ = factory()
    model = VisionModel()
    enabled = VisionService(VisionSettings(video_frame_count=2), image_reader(), model, store, model_name="v")
    old = enabled.enrich(video_document(now), now)
    disabled = VisionService(settings, image_reader(), model, store, model_name="v")
    result = disabled.enrich(old, now + 1, previous=old)
    assert result.evidence_level == "transcript" and not result.images and result.visual.status == "disabled"
    assert enabled.needs_refresh(result, now + 2)


def test_visual_failure_and_absent_snapshot_keep_correct_text_evidence(factory, now):
    _, store, _, _ = factory()
    service = VisionService(VisionSettings(), image_reader(), VisionModel(fail=True), store, model_name="v")
    doc = service.enrich(video_document(now), now)
    assert doc.evidence_level == "transcript" and doc.visual.status == "failed" and not doc.visual.indices
    absent = replace(video_document(now), images=(), frame_context=FrameContext("unavailable", created_at=now, retry_after=now + 600))
    assert service.enrich(absent, now).visual.status == "unavailable"
    assert not service.needs_refresh(absent, now + 1) and service.needs_refresh(absent, now + 601)


def test_frame_success_reaches_comment_reply_and_browsing_decision(factory, now):
    engine, store, model, platform = factory()
    platform.documents[REF.key] = video_document(now)
    platform.ref = REF
    engine.vision = VisionService(VisionSettings(video_frame_count=2), image_reader(), VisionModel(), store, model_name="v")
    engine.ingest(Event("frame-at", "bilibili", "robot", "123", "video-thread", "mention", "这个画面如何", now,
                        content=REF, reply_to="700", thread_root="700"))
    assert engine.process_one_event(now)
    payload = next(payload for op, payload in model.calls if op == "reply")
    assert payload["content"]["visual"]["indices"] == (1, 2)
    assert payload["content"]["images"][1]["timestamp_seconds"] == 70
    assert engine.browse(now + 1)  # the ordinary autonomous path uses the same enriched document
    payload = next(payload for op, payload in model.calls if op == "evaluate_video")
    assert "70秒" in payload["content"]["visual"]["summary"]


def test_sensitive_visual_evidence_still_triggers_mandatory_topic_filter(factory, now):
    _, store, _, _ = factory()
    service = VisionService(VisionSettings(), image_reader(), VisionModel(), store, model_name="v")
    doc = service.enrich(video_document(now), now)
    assert document_topic(replace(doc, visual=replace(doc.visual, summary="画面出现政治宣传"))) == "sensitive"


@pytest.mark.parametrize("sensitive", [False, True])
def test_adapter_uses_only_snapshot_endpoint_and_skips_sensitive_metadata(tmp_path, sensitive):
    paths = []
    def handle(request):
        paths.append(request.url.path)
        if request.url.path.endswith("/view"):
            data = {"bvid": "BVexample", "title": "政治" if sensitive else "角色舞蹈", "desc": "画面演示",
                    "owner": {"mid": 123}, "pages": [{"cid": 12, "duration": 80}]}
        elif request.url.path.endswith("/videoshot"):
            assert request.url.params["index"] == "1"
            data = shot()
        else:
            data = {"replies": []}
        return httpx.Response(200, json={"code": 0, "data": data})
    adapter = BilibiliAdapter(credentials(tmp_path), read_video_subtitles=False, video_frames_enabled=True,
                               client=httpx.Client(transport=httpx.MockTransport(handle)))
    doc = adapter.read(REF)
    assert not any("playurl" in p for p in paths)
    if sensitive:
        assert not doc.images and paths == ["/x/web-interface/view"]
    else:
        assert len(doc.images) == 6 and "/x/player/videoshot" in paths


def test_old_cached_video_is_refreshed_after_enabling_frames(factory, now):
    engine, store, _, platform = factory()
    store.cache_document(Document(REF, "旧标题", "简介", "metadata"), now)
    platform.documents[REF.key] = video_document(now)
    engine.vision = VisionService(VisionSettings(video_frame_count=2), image_reader(), VisionModel(), store, model_name="v")
    assert engine.document(REF, now + 1).visual.indices == (1, 2)


def test_gateway_labels_frames_and_sends_pixels_without_sprites(factory, now):
    _, store, _, _ = factory()
    pixels = image_reader().load(video_document(now).images[1], 4096)
    def handle(request):
        body = json.loads(request.content)
        blocks = body["messages"][-1]["content"]
        assert "P2" in blocks[-2]["text"] and "30秒" in blocks[-2]["text"]
        assert blocks[-1]["image_url"]["url"] == pixels.data_url
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"summary":"蓝色画面"}'}}]})
    model = ChatCompletionsModel(base_url="https://model.invalid", name="v", api_key="", store=store,
                                 timezone="Asia/Hong_Kong", daily_limit=10,
                                 client=httpx.Client(transport=httpx.MockTransport(handle)))
    model.decide("describe_images", {"media_kind": "video_frames", "_image_inputs": [
        {"index": 1, "label": "视频P2，30秒", "data_url": pixels.data_url}]})


@pytest.mark.parametrize("key,value", [("video_frame_count", 0), ("video_frame_count", 10),
                                       ("video_frame_max_parts", 4), ("video_frames_enabled", "true")])
def test_invalid_frame_configuration_is_rejected(key, value):
    config = yaml.safe_load((Path(__file__).parents[1] / "config.yml").read_text(encoding="utf-8"))
    config["vision"][key] = value
    with pytest.raises(ValueError):
        Config.validate(config)


def test_frame_controls_save_and_runtime_respects_global_image_limit(tmp_path):
    from test_architecture_and_config import write_config
    from murasamebot.bootstrap.runtime import build
    path = write_config(tmp_path)
    config = Config(path)
    config.save_local({"vision": {"video_frames_enabled": True, "video_frame_count": 8,
                                  "video_frame_max_parts": 2, "max_images": 4}})
    runtime = build(Config(path))
    try:
        assert runtime.platform.video_frames_enabled
        assert runtime.platform.video_frames.count == 4 and runtime.platform.video_frames.max_parts == 2
    finally:
        runtime.close()
    config = Config(path)
    config.save_local({"vision": {"enabled": False}})
    runtime = build(Config(path))
    try:
        assert not runtime.platform.video_frames_enabled
    finally:
        runtime.close()


def test_partial_crop_failure_sends_only_successful_timestamps(factory, now):
    _, store, _, _ = factory()
    model = VisionModel()
    doc = video_document(now)
    doc = replace(doc, images=(doc.images[0], replace(doc.images[1], crop=(30, 30, 16, 16))))
    service = VisionService(VisionSettings(), image_reader(), model, store, model_name="v")
    result = service.enrich(doc, now)
    assert result.visual.status == "partial" and result.visual.indices == (1,)
    assert [s["timestamp_seconds"] for s in model.calls[0][1]["frame_samples"]] == [10]


def test_frame_batch_context_is_isolated_for_concurrent_conversations():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    barrier, calls = Barrier(2), []
    reader = image_reader(calls)
    def read_conversation(_):
        with reader.batch(10000):
            barrier.wait(timeout=5)
            for n in (1, 2):
                reader.load(ImageRef("https://i0.hdslb.com/a", n, (0, 0, 16, 16)), 4096)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(read_conversation, (1, 2)))
    assert len(calls) == 2


def test_slow_snapshot_does_not_hold_a_global_conversation_lock():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    barrier = Barrier(2)
    def request(*args, **kwargs):
        barrier.wait(timeout=5)
        return shot(), None
    sampler = VideoShotReader(request)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(sampler.read, [{"bvid": b, "pages": [{"cid": 1}]} for b in ("BVone", "BVtwo")]))
    assert all(len(images) == 6 for images, context in results)


def test_injected_redirect_enabled_client_cannot_follow_sprite_redirects():
    calls = []
    def handle(request):
        calls.append(request.url.host)
        return httpx.Response(302, headers={"Location": "https://evil.invalid/private"})
    reader = HTTPImageReader(hosts=("i0.hdslb.com",),
                             client=httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=True))
    with pytest.raises(MediaError):
        reader.load(ImageRef("https://i0.hdslb.com/a", 1, (0, 0, 16, 16)), 4096)
    assert calls == ["i0.hdslb.com"]
