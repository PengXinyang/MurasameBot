from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import re
from zoneinfo import ZoneInfo

from murasamebot.domain.models import Action, Artwork, ModelError, Persona, PlatformError, PolicyError, stable_id
from murasamebot.ports import ArtworkSource, ModelGateway, StateStore


@dataclass(frozen=True)
class ArtSettings:
    enabled: bool = False
    delivery: str = "image"
    times: tuple[str, ...] = ("12:30", "20:30")
    weekdays: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6)
    grace_minutes: int = 30
    candidate_limit: int = 8
    max_caption_characters: int = 100
    keywords: tuple[str, ...] = ("千恋万花", "丛雨")
    sources: tuple[str, ...] = ("pixiv",)
    permissions: tuple[dict, ...] = ()

    @classmethod
    def from_config(cls, data: dict) -> ArtSettings:
        fields = cls.__dataclass_fields__
        values = {k: v for k, v in data.items() if k in fields}
        for key in ("times", "weekdays", "keywords", "sources", "permissions"):
            if key in values:
                values[key] = tuple(values[key])
        return cls(**values)

    def permission(self, artwork: dict, now: float) -> dict | None:
        ref = artwork.get("ref", {})
        for grant in self.permissions:
            if grant["source"] != ref.get("provider") or str(grant["artist_id"]) != artwork.get("artist_id"):
                continue
            if grant.get("work_ids") and str(ref.get("external_id")) not in {str(i) for i in grant["work_ids"]}:
                continue
            if grant.get("expires_at") and datetime.fromisoformat(str(grant["expires_at"])).timestamp() <= now:
                continue
            return grant
        return None

    def slot(self, now: float, timezone: str) -> tuple[str, float] | None:
        current = datetime.fromtimestamp(now, ZoneInfo(timezone))
        slots = []
        # A slot just before midnight may remain within its grace interval.
        for delta in (0, 1):
            day = current.date() - timedelta(days=delta)
            if day.weekday() not in self.weekdays:
                continue
            for value in self.times:
                moment = datetime.fromisoformat(f"{day.isoformat()}T{value}").replace(tzinfo=current.tzinfo)
                stamp = moment.timestamp()
                if 0 <= now - stamp < self.grace_minutes * 60:
                    slots.append((moment.isoformat(), stamp))
        return max(slots, key=lambda item: item[1]) if slots else None


BLOCKED = re.compile(r"r[-_ ]?18|nsfw|裸|色情|性器|宣伝|告知|宣传|販促|预约|予約|発売|发售|グッズ|commission\s*open|ai生成|ai.?generated", re.I)


def eligible(work: Artwork) -> bool:
    # Unknown ratings and missing provenance fail closed. Metadata is not a
    # visual moderation guarantee; source moderation + human draft review remain useful.
    metadata = " ".join((work.title, work.description, *work.tags))
    return bool(work.safe and not work.is_ai and work.artist_id and work.artist_name and
                work.image_url and work.source_url and not BLOCKED.search(metadata))


class ArtDiscovery:
    """Called by the shared worker, never its own reply loop or sender."""

    def __init__(self, settings: ArtSettings, sources: dict[str, ArtworkSource], store: StateStore,
                 model: ModelGateway, persona: Persona):
        self.settings, self.sources, self.store, self.model, self.persona = settings, sources, store, model, persona

    def run(self, now: float, *, agent_id: str, platform: str, account_id: str, timezone: str,
            draft: bool, ttl_minutes: int) -> bool:
        if not self.settings.enabled or self.store.get_setting("paused", False) or self.store.get_setting(f"platform_paused:{platform}", False):
            return False
        slot = self.settings.slot(now, timezone)
        if slot is None or not self.store.acquire_lease("art_discovery", now, 900):
            return False
        try:
            key = f"art_slot:{slot[0]}"
            if self.store.get_setting(key, False):
                return False
            # Claim before slow work; a crash never causes a burst of duplicate generations.
            self.store.set_setting(key, True)
            index = int(self.store.get_setting("art_query_cursor", 0)) % len(self.settings.keywords)
            self.store.set_setting("art_query_cursor", index + 1)
            # One rotating keyword per slot bounds source traffic even when every
            # search result is filtered out before the model is called.
            keywords = (self.settings.keywords[index],)
            remaining = self.settings.candidate_limit
            for source in self.settings.sources:
                provider = self.sources.get(source)
                if provider is None:
                    continue
                for keyword in keywords:
                    try:
                        works = provider.search_artworks(keyword, self.settings.candidate_limit)
                    except PlatformError as exc:
                        self.store.audit("art_source_error", f"{source}: {exc}", now)
                        break  # Other sources still get a chance.
                    for work in works:
                        if not eligible(work) or self.store.has_action_for_content(work.ref.key, now):
                            continue
                        grant = self.settings.permission(work.to_dict(), now)
                        if self.settings.delivery == "image" and not grant:
                            continue
                        remaining -= 1
                        result = self.model.decide("caption_artwork", {
                            "persona": self.persona.prompt(), "keyword": keyword,
                            "title": work.title, "artist": work.artist_name, "tags": list(work.tags),
                            "description": work.description[:1000], "evidence": "metadata_only",
                            "max_characters": self.settings.max_caption_characters,
                        })
                        if result.get("decision") != "share" or result.get("fanart") is not True:
                            if remaining <= 0:
                                return False
                            continue
                        from murasamebot.application.engine import checked_text
                        text = checked_text(result.get("text"), self.settings.max_caption_characters)
                        details = work.to_dict()
                        details["delivery"] = self.settings.delivery
                        details["permission_id"] = stable_id(grant) if grant else ""
                        if self.settings.delivery == "local":
                            self.store.set_setting("artwork:last_discovery", {"artwork": details, "text": text, "at": now})
                            self.store.audit("art_collected", f"已收集：{work.source_url}；{text}", now)
                            return True
                        action = Action(stable_id(agent_id, "artwork", work.ref.key, slot[0]), agent_id, platform, account_id,
                                        work.ref, text, now, now + ttl_minutes * 60, kind="artwork_share",
                                        evidence_level="metadata", dedup_key=f"artwork:{work.ref.key}", artwork=details,
                                        decision_reason="已筛选角色插画；图片转载仅限配置授权范围")
                        return self.store.enqueue_action(action, draft=draft, now=now)
            self.store.audit("art_skipped", "本时段未找到符合关键词、内容筛选与转载授权的角色插画", now)
        except (ModelError, PolicyError, ValueError, KeyError) as exc:
            self.store.audit("art_error", f"{type(exc).__name__}: {exc}", now)
        finally:
            self.store.release_lease("art_discovery")
        return False
