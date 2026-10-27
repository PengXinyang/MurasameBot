from dataclasses import asdict, dataclass
import re
from collections.abc import Mapping

from murasamebot.domain.models import Emoji, PlatformError, stable_id
from murasamebot.ports import EmojiCatalog, StateStore


UNICODE_EMOJIS = tuple(Emoji(f"unicode:{i}", token, label, "unicode") for i, (token, label) in enumerate((
    ("😊", "开心、温和鼓励"), ("🤔", "思考、好奇"), ("👏", "赞赏、祝贺"),
    ("🥰", "亲近、喜爱"), ("😢", "难过、共情"), ("🙏", "感谢、请求"))))

KAOMOJIS = tuple(Emoji(f"kaomoji:{name}", token, label, "kaomoji") for name, token, label in (
    ("happy", "(*´▽｀*)", "开心、温柔的笑"), ("excited", "(≧▽≦)", "兴奋、雀跃"),
    ("wink", "(｡•̀ᴗ-)✧", "俏皮、轻松回应"), ("cheer", "(๑•̀ㅂ•́)و✧", "鼓励、一起加油"),
    ("concern", "(´･ω･`)", "关心、认真倾听"), ("sad", "(｡•́︿•̀｡)", "难过、轻轻共情"),
    ("shy", "(〃▽〃)", "害羞、受到夸奖"), ("determined", "(•̀ᴗ•́)و", "认真、充满干劲")))

FACE_CANDIDATE = re.compile(r"[（(][^\r\n()（）\u4e00-\u9fff]{1,32}[）)](?:[و♡✧]+)?")


def strip_inline_expressions(text):
    # Selected faces are appended once, counted together with native emotes.
    for emoji in sorted(KAOMOJIS,key=lambda e:len(e.token),reverse=True):
        text = text.replace(emoji.token, "")
    text = FACE_CANDIDATE.sub(lambda m: "" if re.search(r"[ωᴗ▽◕ಠ•･´｀￣≧≦｡〃ﾟДдㅂ︿╥]|\^[_-]?\^",m[0]) else m[0],text)
    return INLINE_EMOJI.sub("",text).strip()

# Remove model-authored inline emotes; only catalog selections are rendered.
INLINE_EMOJI = re.compile(r"\[[^\[\]\r\n]{1,100}\]|[#*0-9]\ufe0f?\u20e3|[\U0001f000-\U0001faff\u2600-\u27bf](?:[\ufe0e\ufe0f]|[\U0001f3fb-\U0001f3ff])*(?:\u200d[\U0001f000-\U0001faff\u2600-\u27bf][\ufe0e\ufe0f\U0001f3fb-\U0001f3ff]*)*")


@dataclass(frozen=True)
class ExpressionSettings:
    enabled: bool = True
    max_per_message: int = 2
    cache_minutes: int = 360
    preferred_keywords: tuple[str, ...] = ("千恋万花", "丛雨")
    kaomoji_enabled: bool = True


class ExpressionService:
    """Channel-neutral expression choices; catalog keys never select a recipient."""

    def __init__(self, settings: ExpressionSettings, sources: Mapping[str, EmojiCatalog], store: StateStore):
        self.settings, self.sources, self.store = settings, dict(sources), store

    def options(self, platform: str, account_id: str, channel: str, now: float) -> tuple[Emoji, ...]:
        if not self.settings.enabled:
            return ()
        faces = KAOMOJIS if self.settings.kaomoji_enabled else ()
        if channel == "private":
            return (*faces,*UNICODE_EMOJIS)
        source = self.sources.get(platform)
        if source is None:
            return faces
        key = f"emoji_catalog:{platform}:{account_id}:{channel}:{stable_id(self.settings.preferred_keywords)}"
        cached = self.store.get_setting(key)
        if cached and now < cached["expires_at"]:
            options = tuple(Emoji(**item) for item in cached["items"])
            return (*self.preferred(options),*faces)
        try:
            options = tuple(source.emoji_options(account_id, channel))
            options = self.preferred(options)
            self.store.set_setting(key, {"items": [asdict(e) for e in options], "expires_at": now+self.settings.cache_minutes*60})
            self.store.audit("emoji_catalog", f"{platform}/{channel}：读取可用表情 {len(options)} 个", now)
            return (*options,*faces)
        except PlatformError as exc:
            if exc.pause:
                raise
            self.store.audit("emoji_warning", "原生表情列表读取失败，本次仅保留已启用的文字颜文字", now)
            self.store.set_setting(key, {"items": [], "expires_at": now+60})
            return faces

    def preferred(self, options):
        # Match the package before the character: other Senren Banka characters
        # still precede unrelated Congyu-labelled packs and generic emotes.
        return tuple(sorted(options,key=lambda e:tuple(k not in e.token for k in self.settings.preferred_keywords)))[:24]

    def compose(self, result: dict, options: tuple[Emoji, ...], limit: int) -> tuple[str, tuple[Emoji, ...]]:
        text = result.get("text")
        if not isinstance(text, str):
            return text, ()
        text = strip_inline_expressions(text)
        selected, available = [], {e.key: e for e in options}
        requested = result.get("emojis", [])
        if self.settings.enabled and isinstance(requested, list):
            for key in requested:
                if isinstance(key, str) and key in available and available[key].token not in {e.token for e in selected}:
                    emoji = available[key]
                    if emoji.kind=="kaomoji" and (not self.settings.kaomoji_enabled or any(e.kind=="kaomoji" for e in selected)):
                        continue
                    if len(text) + sum(len(e.token) for e in selected) + len(emoji.token) <= limit:
                        selected.append(emoji)
                if len(selected) >= self.settings.max_per_message:
                    break
        return text, tuple(selected)
