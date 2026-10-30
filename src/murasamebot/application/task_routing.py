"""Program-owned delivery selection; model output never selects an account."""
import re

from murasamebot.domain.models import PolicyError


def owner_video_route(store, event, person, *, provider, private_platform, public_platform, accounts):
    public = bool(re.search(r"(?:[@＠]|艾特|提及)\s*(?:我|主人)", event.text))
    private = bool(re.search(r"(?:发|分享|回|传).{0,8}(?:QQ|qq|这里|私聊)", event.text))
    if re.search(r"(?:不要|别|不用|不必)[^\n，。；,;]{0,8}(?:[@＠]|艾特|提及)\s*(?:我|主人)",event.text):
        public = False
    if re.search(r"(?:不要|别|不用|不必)[^\n，。；,;]{0,8}(?:发|分享|回|传).{0,8}(?:QQ|qq|这里|私聊)",event.text):
        private = False
    if public and private:
        raise PolicyError("这条委托同时指定了两个投递渠道，请选择QQ回传或B站提及")
    platform = public_platform if public else private_platform
    binding = store.binding(person, platform)
    source = store.binding(person, event.platform)
    if not binding or binding["purpose"] != "owner_video_recommendation" or not source:
        raise PolicyError("尚未绑定允许投递的目标账号")
    if source["external_id"] != event.actor_id:
        raise PolicyError("委托来源账号已变化")
    return {"provider": provider, "platform": platform, "mode": "mention" if public else "private",
        "account_id": accounts.get(platform, ""), "recipient_id": binding["external_id"],
        "conversation_id": event.conversation_id if not public else "",
        "binding_version": binding["version"], "source_platform": event.platform,
        "source_actor": event.actor_id, "source_account":event.account_id, "source_binding_version": source["version"]}
