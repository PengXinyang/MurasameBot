"""OneBot 11 private-chat adapter; only the verified owner is allowed."""
import time
from urllib.parse import urlparse

import httpx

from murasamebot.domain.models import Event, ImageRef, PlatformError, Receipt, stable_id

IMAGE_HOSTS = ("multimedia.nt.qq.com", "multimedia.nt.qq.com.cn", "gchat.qpic.cn", "c2cpicdw.qpic.cn")


class QQAdapter:
    def __init__(self, settings, store, owner_person_id, *, client=None):
        self.settings, self.store, self.owner = settings, store, owner_person_id
        self.account_id = str(settings["robot_id"])
        self.client = client or httpx.Client(base_url=settings["api_url"], timeout=15, trust_env=False,
            headers={"Authorization": "Bearer " + settings["access_token"]})

    @property
    def configured(self):
        return bool(self.account_id and self.settings["access_token"] and self.settings["callback_secret"])

    def api(self, action, payload=None, *, writing=False):
        if action not in {"get_login_info", "get_friend_list", "send_private_msg"}:
            raise ValueError("unsupported QQ API")
        try:
            response = self.client.post("/" + action, json=payload or {})
            response.raise_for_status()
            result = response.json()
        except (httpx.HTTPError, ValueError):
            if writing:
                return None
            raise PlatformError("QQ接口不可用", platform="qq", code="qq_unavailable") from None
        if not isinstance(result, dict) or result.get("retcode") != 0 or result.get("status") != "ok":
            if writing and isinstance(result, dict) and result.get("status") == "failed":
                return {"failed": True}
            if writing:
                return None
            raise PlatformError("QQ只读检查失败", platform="qq", code="qq_check_failed")
        return result.get("data")

    def account(self):
        data = self.api("get_login_info")
        if not isinstance(data, dict) or str(data.get("user_id")) != self.account_id:
            raise PlatformError("QQ机器人账号不匹配", platform="qq", code="qq_wrong_account", pause=True)
        return {"uid": self.account_id}

    def allows_private_reply(self, account_id, peer_id):
        binding = self.store.binding(self.owner, "qq")
        if (not binding or binding["external_id"] != str(peer_id) or str(account_id) != self.account_id
                or str(peer_id) == self.account_id):
            return False
        self.account()
        friends = self.api("get_friend_list")
        return isinstance(friends, list) and any(str(f.get("user_id")) == str(peer_id) for f in friends if isinstance(f, dict))

    def parse(self, payload, now=None):
        now = time.time() if now is None else now
        if (payload.get("post_type") != "message" or payload.get("message_type") != "private"
                or payload.get("sub_type") != "friend" or str(payload.get("self_id")) != self.account_id):
            return None
        peer = str(payload.get("user_id", ""))
        if not self.allows_private_reply(self.account_id, peer):
            return None
        segments = payload.get("message")
        if not isinstance(segments, list) or len(segments) > 100:
            raise ValueError("QQ需上报Array格式消息段")
        text, images = [], []
        for segment in segments:
            data = segment.get("data", {})
            if segment.get("type") == "text":
                text.append(str(data.get("text", "")))
            elif segment.get("type") == "image":
                url = str(data.get("url", ""))
                parsed = urlparse(url)
                if parsed.scheme == "https" and parsed.hostname in IMAGE_HOSTS and not parsed.username and parsed.port in {None, 443}:
                    images.append(ImageRef(url, len(images) + 1))
                    text.append(f"[图片{len(images)}]")
                else:
                    text.append("[图片来源不可读取]")
            else:
                text.append("[暂不支持的消息类型]")
        message_id = str(payload.get("message_id", ""))
        if not message_id or not text or len("".join(text)) > 12000 or len(images) > 9:
            raise ValueError("QQ消息标识、大小或图片数量无效")
        return Event(message_id, "qq", self.account_id, peer, f"qq:{self.account_id}:{peer}",
            "private_message", "".join(text), now, "private", images=tuple(images), reply_limit=self.settings["max_reply_messages"])

    @staticmethod
    def private_conversation(account_id, peer_id):
        return stable_id("conversation", "qq", account_id, f"qq:{account_id}:{peer_id}")

    def pending_private_activity(self, account_id, peer_id, now):
        from murasamebot.adapters.persistence.chat_batches import ChatBatches
        binding = self.store.binding(self.owner, "qq")
        if not binding or binding["external_id"] != peer_id or account_id != self.account_id:
            return {"pending": 1, "last_at": now}
        return ChatBatches(self.store).activity(account_id, peer_id, binding["version"], now)

    def send(self, action, mentions, *, before_write=None):
        if action.kind not in {"private_reply", "task_result", "proactive_private"} or action.platform != "qq" or mentions or not self.allows_private_reply(action.account_id, action.recipient_id):
            return Receipt("failed", detail="QQ收件人或好友权限无效")
        if before_write and not before_write():
            return Receipt("failed", detail="发送前策略校验失败")
        result = self.api("send_private_msg", {"user_id": int(action.recipient_id),
            "message": [{"type": "text", "data": {"text": action.text}}]}, writing=True)
        if result is None or not isinstance(result, dict) or not result.get("message_id"):
            return Receipt("failed" if isinstance(result, dict) and result.get("failed") else "unknown", detail="QQ未取得确定发送回执，禁止自动重发")
        return Receipt("accepted", str(result["message_id"]), "OneBot受理，客户端送达待验证")

    def close(self):
        self.client.close()
