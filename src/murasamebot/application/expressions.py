from dataclasses import asdict, dataclass
import re
from collections.abc import Mapping

from murasamebot.domain.models import Emoji, PlatformError, stable_id
from murasamebot.ports import EmojiCatalog, StateStore


UNICODE_EMOJIS = tuple(Emoji(f"unicode:{i}", token, label, "unicode") for i, (token, label) in enumerate((
    ("😊", "开心、温和鼓励"), ("🤔", "思考、好奇"), ("👏", "赞赏、祝贺"),
    ("🥰", "亲近、喜爱"), ("😢", "难过、共情"), ("🙏", "感谢、请求"))))

# Remove model-authored inline emotes; only catalog selections are rendered.
INLINE_EMOJI = re.compile(r"\[[^\[\]\r\n]{1,100}\]|[#*0-9]\ufe0f?\u20e3|[\U0001f000-\U0001faff\u2600-\u27bf](?:[\ufe0e\ufe0f]|[\U0001f3fb-\U0001f3ff])*(?:\u200d[\U0001f000-\U0001faff\u2600-\u27bf][\ufe0e\ufe0f\U0001f3fb-\U0001f3ff]*)*")


@dataclass(frozen=True)
class ExpressionSettings:
    enabled: bool = True
    max_per_message: int = 2
    cache_minutes: int = 360
    preferred_keywords: tuple[str, ...] = ("丛雨",)


class ExpressionService:
    """Channel-neutral expression choices; catalog keys never select a recipient."""

    def __init__(self, settings: ExpressionSettings, sources: Mapping[str, EmojiCatalog], store: StateStore):
        self.settings, self.sources, self.store = settings, dict(sources), store

    def options(self, platform: str, account_id: str, channel: str, now: float) -> tuple[Emoji, ...]:
        if not self.settings.enabled:
            return ()
        if channel == "private":
            return UNICODE_EMOJIS
        source = self.sources.get(platform)
        if source is None:
            return ()
        key = f"emoji_catalog:{platform}:{account_id}:{channel}:{stable_id(self.settings.preferred_keywords)}"
        cached = self.store.get_setting(key)
        if cached and now < cached["expires_at"]:
            options = tuple(Emoji(**item) for item in cached["items"])
            return tuple(sorted(options, key=lambda e: not any(k in e.token for k in self.settings.preferred_keywords)))[:24]
        try:
            options = tuple(source.emoji_options(account_id, channel))
            options = tuple(sorted(options, key=lambda e: not any(k in e.token for k in self.settings.preferred_keywords)))[:24]
            self.store.set_setting(key, {"items": [asdict(e) for e in options], "expires_at": now+self.settings.cache_minutes*60})
            self.store.audit("emoji_catalog", f"{platform}/{channel}：读取可用表情 {len(options)} 个", now)
            return options
        except PlatformError as exc:
            if exc.pause:
                raise
            self.store.audit("emoji_warning", "表情列表读取失败，本次使用纯文字", now)
            self.store.set_setting(key, {"items": [], "expires_at": now+60})
            return ()

    def compose(self, result: dict, options: tuple[Emoji, ...], limit: int) -> tuple[str, tuple[Emoji, ...]]:
        text = result.get("text")
        if not isinstance(text, str):
            return text, ()
        text = INLINE_EMOJI.sub("", text).strip()
        selected, available = [], {e.key: e for e in options}
        requested = result.get("emojis", [])
        if self.settings.enabled and isinstance(requested, list):
            for key in requested:
                if isinstance(key, str) and key in available and available[key].token not in {e.token for e in selected}:
                    emoji = available[key]
                    if len(text) + sum(len(e.token) for e in selected) + len(emoji.token) <= limit:
                        selected.append(emoji)
                if len(selected) >= self.settings.max_per_message:
                    break
        return text, tuple(selected)
