import re
import time

from murasamebot.application.expressions import UNICODE_EMOJIS
from murasamebot.domain.models import Emoji, PlatformError, Receipt


class BilibiliEmojiMixin:
    def emoji_options(self, account_id: str, channel: str):
        if not self.configured or account_id != (self._account or self.account())["uid"]:
            raise PlatformError("表情列表属于其他账号", platform="bilibili")
        if channel == "private":
            return UNICODE_EMOJIS
        if channel not in {"public", "dynamic"}:
            return ()
        data, _ = self._request("/x/emote/user/panel/web", params={"business": "dynamic" if channel == "dynamic" else "reply"})
        result = []
        for pack in data.get("packages") or []:
            flags = pack.get("flags") or {}
            if flags.get("no_access") or flags.get("disabled") or flags.get("added") is False:
                continue
            expiry = pack.get("expire_time") or 0
            if not isinstance(expiry, (int, float)) or expiry and expiry <= time.time():
                continue
            for item in pack.get("emote") or []:
                flags = item.get("flags") or {}
                token, identifier = item.get("text"), item.get("id")
                if (flags.get("no_access") or flags.get("disabled") or type(identifier) is not int or
                    not isinstance(token, str) or not re.fullmatch(r"\[[^\[\]\r\n<>@]{1,80}\]", token)):
                    continue
                result.append(Emoji(f"bilibili:{identifier}", token, str((item.get("meta") or {}).get("alias") or token)[0:100]))
        return tuple(result)

    def check_emojis(self, action):
        if not action.emojis:
            return None
        if len(action.emojis) > 2:
            return Receipt("failed", detail="每条消息最多使用 2 个表情")
        channel = "private" if action.visibility == "private" else "dynamic" if action.kind in {"owner_repost", "artwork_share"} else "public"
        allowed = {e.key: e for e in self.emoji_options(action.account_id, channel)}
        if any(allowed.get(e.key) != e or action.text.count(e.token) != 1 for e in action.emojis):
            return Receipt("failed", detail="表情不可用、权限已变化或正文与已审核表情不一致")
        return None
