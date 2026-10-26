from dataclasses import replace
import json

import httpx
import pytest
import yaml

from murasamebot.adapters.artwork.sources import PixivSource, XSource
from murasamebot.application.artwork import ArtDiscovery, ArtSettings, eligible
from murasamebot.bootstrap.config import Config
from murasamebot.bootstrap.runtime import build
from murasamebot.domain.models import Artwork, ContentRef, stable_id
from test_architecture_and_config import write_config


GRANT = {"source": "pixiv", "artist_id": "123", "evidence": "测试画师书面允许署名转载", "work_ids": ["456"]}
WORK = Artwork(ContentRef("pixiv", "artwork", "456"), "丛雨同人", "123", "测试画师",
               "https://www.pixiv.net/artworks/456", "https://i.pximg.net/img-original/test.png",
               ("千恋万花", "丛雨"), safe=True)


class Source:
    def __init__(self, works=(WORK,)):
        self.works = works
        self.calls = []

    def search_artworks(self, keyword, limit):
        self.calls.append(keyword)
        return list(self.works)[:limit]

    def close(self):
        pass


def discovery(engine, store, model, works=(WORK,), permissions=(GRANT,), **kwargs):
    calls = []
    def decide(operation, payload):
        calls.append((operation, payload))
        return {"decision": "share", "fanart": True, "text": "吾辈寻到一张角色画作，想与你分享这份小欢喜。"}
    model.decide = decide
    settings = ArtSettings(enabled=True, times=("19:30", "20:30"), permissions=permissions, **kwargs)
    source = Source(works)
    return ArtDiscovery(settings, {"pixiv": source}, store, model, engine.persona), calls, source


def run(service, now, draft=False):
    return service.run(now, agent_id=service.store.agent_id, platform="bilibili", account_id="robot",
                       timezone="Asia/Hong_Kong", draft=draft, ttl_minutes=30)


def test_authorized_image_goes_through_shared_outbox_and_restart_dedup(factory, now):
    engine, store, model, platform = factory()
    service, calls, source = discovery(engine, store, model)
    assert run(service, now)
    row = store.inspect("actions")[0]
    action = json.loads(row["payload"])
    assert action["artwork"]["permission_id"] == stable_id(GRANT)
    assert action["artwork"]["source_url"] == WORK.source_url
    assert platform.sent == []
    assert calls[0][1]["evidence"] == "metadata_only"
    assert "memories" not in calls[0][1] and "api_key" not in str(calls)
    # Reconstructing the scheduler cannot repeat the same slot after a restart.
    restarted, _, _ = discovery(engine, store, model)
    assert not run(restarted, now + 30)
    assert engine.dispatch_one(now)
    assert not run(restarted, now + 3600)  # Same artwork is not reposted next slot.
    assert len(store.inspect("actions")) == 1


@pytest.mark.parametrize("works,permissions", [
    ((WORK,), ()),
    ((replace(WORK, ref=ContentRef("pixiv", "artwork", "999")),), (GRANT,)),
    ((replace(WORK, safe=False),), (GRANT,)),
    ((replace(WORK, is_ai=True),), (GRANT,)),
    ((replace(WORK, title="新游戏发售宣传图"),), (GRANT,)),
    ((WORK,), ({**GRANT, "expires_at": "2026-10-25T00:00:00+08:00"},)),
])
def test_unauthorized_unknown_rating_promotional_and_ai_images_never_queue(factory, now, works, permissions):
    engine, store, model, _ = factory()
    service, calls, _ = discovery(engine, store, model, works, permissions)
    assert not run(service, now)
    assert store.inspect("actions") == [] and calls == []


def test_stale_schedule_is_not_replayed_and_midnight_grace_belongs_previous_day(now):
    settings = ArtSettings(times=("19:30",), weekdays=(0,))
    assert settings.slot(now + 1800, "Asia/Hong_Kong") is None
    midnight = ArtSettings(times=("23:55",), weekdays=(0,))
    assert midnight.slot(now + 4.5 * 3600, "Asia/Hong_Kong") is not None


def test_expired_unsent_artwork_can_be_proposed_in_later_slot(factory, now):
    engine, store, model, platform = factory()
    service, _, _ = discovery(engine, store, model)
    assert run(service, now, draft=True)
    assert run(service, now + 3600, draft=True)
    assert len({row["action_id"] for row in store.inspect("actions")}) == 2


def test_revoked_permission_blocks_already_approved_image(factory, tmp_path, now):
    path = write_config(tmp_path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["mode"] = "auto"
    data["artwork"].update(enabled=True, times=["19:30"], permissions=[GRANT])
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    runtime = build(Config(path))
    _, _, model, sink = factory(agent_id="sink-agent")
    try:
        service, _, _ = discovery(runtime.engine, runtime.store, model)
        assert run(service, now)
        runtime.engine.sinks["bilibili"] = sink
        # Remove the permission from disk after the action has been generated.
        data["artwork"]["permissions"] = []
        path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        assert runtime.engine.dispatch_one(now)
        assert sink.sent == []
        assert runtime.store.inspect("actions")[0]["state"] == "failed"
    finally:
        runtime.close()


def test_pixiv_safe_search_reads_actual_artist_and_original_image_not_advertisement():
    requests = []
    def handle(request):
        requests.append(request)
        if "/search/" in request.url.path:
            assert request.url.params["mode"] == "safe"
            return httpx.Response(200, json={"error": False, "body": {"illust": {"data": [{"id": "456"}, {"isAdContainer": True}]}}})
        return httpx.Response(200, json={"error": False, "body": {
            "illustType": 0, "xRestrict": 0, "aiType": 1, "illustTitle": "丛雨同人", "userId": "123", "userName": "画师",
            "tags": {"tags": [{"tag": "千恋万花"}]}, "urls": {"original": WORK.image_url},
        }})
    source = PixivSource("local-session", client=httpx.Client(transport=httpx.MockTransport(handle)))
    works = source.search_artworks("丛雨", 5)
    assert len(works) == 1 and eligible(works[0])
    assert works[0].artist_id == "123" and works[0].image_url == WORK.image_url
    assert len(requests) == 2 and all(r.url.host == "www.pixiv.net" for r in requests)


def test_x_official_search_requires_photo_author_and_explicit_safe_rating():
    def handle(request):
        assert request.url.host == "api.x.com"
        assert request.headers["Authorization"] == "Bearer api-secret"
        return httpx.Response(200, json={"data": [{"id": "7", "text": "千恋万花 丛雨 #fanart", "author_id": "123", "possibly_sensitive": False,
                                                    "attachments": {"media_keys": ["photo"]}},
                                                   {"id": "8", "text": "未知分级", "author_id": "123", "attachments": {"media_keys": ["photo"]}}],
                                        "includes": {"users": [{"id": "123", "name": "画师", "username": "artist"}],
                                                     "media": [{"media_key": "photo", "type": "photo", "url": "https://pbs.twimg.com/media/test.jpg"}]}})
    source = XSource("api-secret", client=httpx.Client(transport=httpx.MockTransport(handle)))
    works = source.search_artworks("丛雨", 5)
    assert eligible(works[0]) and not eligible(works[1])
    assert works[0].source_url == "https://x.com/artist/status/7"
