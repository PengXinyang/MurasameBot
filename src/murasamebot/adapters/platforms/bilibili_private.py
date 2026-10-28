"""Read-only inbox discovery and gated text replies for Bilibili private messages."""
from __future__ import annotations

from collections.abc import Callable, Mapping
import json
import re
import time
from uuid import uuid4

import httpx

from murasamebot.domain.models import Action, Event, PlatformError, PrivateInbox, Receipt


VC_API = "https://api.vc.bilibili.com"


def failure(message, *, code="private_message_error", pause=False):
    return PlatformError(message, code=code, pause=pause, platform="bilibili")


def number(value) -> int:
    text = str(value)
    return int(text) if re.fullmatch(r"\d{1,24}", text) else 0


class BilibiliPrivateMixin:
    @staticmethod
    def private_conversation(account_id: str, peer_id: str) -> str:
        if not number(account_id) or not number(peer_id):
            raise failure("无效的私信会话标识")
        return f"bilibili:{account_id}:private:{peer_id}"

    def allows_private_reply(self, account_id: str, peer_id: str) -> bool:
        if not self.configured or not number(account_id) or not number(peer_id) or account_id == peer_id:
            return False
        account = self._account or self.account()
        if account["uid"] != account_id:
            return False
        data, _ = self._request("/x/relation", params={"fid": peer_id})
        # Exactly mutual, with the returned UID bound to the requested peer.
        # is_follow, nicknames and cached session relationship hints are not authority.
        return str(data.get("mid", "")) == peer_id and type(data.get("attribute")) is int and data["attribute"] == 6

    @staticmethod
    def private_event(message: dict, account_id: str, peer_id: str) -> Event | None:
        if (str(message.get("sender_uid", "")) != peer_id or peer_id == account_id or
                str(message.get("receiver_id", "")) != account_id or message.get("receiver_type") != 1 or
                message.get("msg_type") != 1 or message.get("msg_status", 0) != 0 or
                message.get("msg_source", 0) not in {0, 1, 2, 3, 4, 7} or message.get("notify_code")):
            return None
        seq, key = number(message.get("msg_seqno")), str(message.get("msg_key") or "")
        if not seq or not number(key) or not number(account_id) or not number(peer_id):
            return None
        try:
            content = json.loads(message.get("content", ""))
            text = content.get("content") if isinstance(content, dict) else None
            stamp = float(message.get("timestamp", 0))
        except (ValueError, TypeError):
            return None
        if not isinstance(text, str) or not text.strip() or len(text) > 6000 or stamp <= 0:
            return None
        return Event(f"bilibili:{account_id}:private:{peer_id}:{key}", "bilibili", account_id, peer_id,
                     f"bilibili:{account_id}:private:{peer_id}", "private_message", text.strip(), stamp,
                     visibility="private", reply_to=key, sequence=seq)

    def _private_sessions(self) -> list[dict]:
        params = {"session_type": 1, "group_fold": 1, "unfollow_fold": 0, "sort_rule": 2,
                  "size": 30, "build": 0, "mobi_app": "web"}
        peers = {}
        for _ in range(self.private_session_pages):
            data, _ = self._request(VC_API + "/session_svr/v1/session_svr/get_sessions", params=params)
            rows = data.get("session_list") or []
            for row in rows:
                if (row.get("session_type") == 1 and not row.get("system_msg_type") and
                        number(row.get("talker_id"))):
                    peers[str(row["talker_id"])] = row
            if not data.get("has_more"):
                return list(peers.values())
            stamps = [number(row.get("session_ts")) for row in rows if number(row.get("session_ts"))]
            end = min(stamps, default=0)
            if not end or (params.get("end_ts") and end >= params["end_ts"]):
                self.warn_inbox("private", "私信会话分页游标未前进，保留已读取的会话并停止翻页。")
                return list(peers.values())
            params["end_ts"] = end
        self.warn_inbox("private", f"私信会话超过 {self.private_session_pages} 页，仅检查已读取的会话。")
        return list(peers.values())

    def private_messages(self, now: float, *, since: float, cursors: Mapping[str, int], baseline=False) -> PrivateInbox:
        uid = (self._account or self.account())["uid"]
        updates, events, denied = {}, {}, 0
        cutoff = max(since, now - self.private_max_age_hours * 3600)
        for session in self._private_sessions():
            peer = str(session["talker_id"])
            if peer == uid:
                continue
            latest = max(number(session.get("max_seqno")), number((session.get("last_msg") or {}).get("msg_seqno")))
            previous = number(cursors.get(peer, 0))
            if baseline:
                updates[peer] = max(previous, latest)
                continue
            if latest and latest <= previous:
                continue
            if not self.allows_private_reply(uid, peer):
                denied += 1
                updates[peer] = max(previous, latest)
                continue
            params = {"talker_id": peer, "session_type": 1, "size": 30, "mobi_app": "web", "build": 0}
            high = previous
            for _ in range(self.private_message_pages):
                data, _ = self._request(VC_API + "/svr_sync/v1/svr_sync/fetch_session_msgs", params=params)
                rows = data.get("messages") or []
                sequences = [number(row.get("msg_seqno")) for row in rows if number(row.get("msg_seqno"))]
                high = max([high, *sequences])
                for row in rows:
                    if number(row.get("msg_seqno")) <= previous:
                        continue
                    event = self.private_event(row, uid, peer)
                    if event and cutoff <= event.occurred_at <= now + 60:
                        events[event.event_id] = event
                oldest = min(sequences, default=0)
                old_times = [float(row.get("timestamp") or 0) for row in rows]
                if not data.get("has_more") or (oldest and oldest <= previous) or (old_times and min(old_times) < cutoff):
                    updates[peer] = high
                    break
                if not oldest or (params.get("end_seqno") and oldest >= params["end_seqno"]):
                    self.warn_inbox("private", "私信消息分页游标未前进，保留已读取的消息；较旧消息不保证补回。")
                    updates[peer] = high
                    break
                params["end_seqno"] = oldest
            else:
                self.warn_inbox("private", f"私信消息超过 {self.private_message_pages} 页，本轮仅保留已读取的消息；较旧消息不再补读。")
                updates[peer] = high
        latest = sorted(events.values(), key=lambda e: (e.occurred_at, e.sequence), reverse=True)
        if len(latest) > self.inbox_batch_limit:
            self.warn_inbox("private", f"本轮私信超过 {self.inbox_batch_limit} 条，仅接收最新 {self.inbox_batch_limit} 条；较旧消息不再补读。")
        return PrivateInbox(tuple(sorted(latest[:self.inbox_batch_limit], key=lambda e: (e.occurred_at, e.sequence))), updates, denied)

    def _private_source_exists(self, action: Action) -> bool:
        if action.source_sequence < 1 or not number(action.reply_to):
            return False
        data, _ = self._request(VC_API + "/svr_sync/v1/svr_sync/fetch_session_msgs", params={
            "talker_id": action.recipient_id, "session_type": 1, "size": 3, "mobi_app": "web",
            "begin_seqno": action.source_sequence - 1, "end_seqno": action.source_sequence + 1,
        })
        for row in data.get("messages") or []:
            event = self.private_event(row, action.account_id, action.recipient_id)
            if event and event.reply_to == action.reply_to and event.sequence == action.source_sequence:
                return True
        return False

    def _send_private(self, action: Action, *, before_write: Callable[[], bool] | None = None) -> Receipt:
        if (action.visibility != "private" or action.content is not None or action.mentions or
                not number(action.recipient_id) or action.recipient_id == action.account_id or
                action.conversation_id != f"bilibili:{action.account_id}:private:{action.recipient_id}"):
            raise failure("私信投递目标或可见性无效")
        encoded = json.dumps({"content": action.text}, ensure_ascii=False, separators=(",", ":"))
        if not action.text.strip() or len(encoded.encode("utf-8")) > 2000:
            raise failure("私信正文为空或超过平台字节限制")
        device = str(uuid4())
        params = self._signed({"w_sender_uid": action.account_id, "w_receiver_id": action.recipient_id, "w_dev_id": device})
        if action.kind == "proactive_private" and (not self.proactive_recipient_id or action.recipient_id != self.proactive_recipient_id or action.event_id or action.reply_to):
            return Receipt("failed", detail="主动私信目标不是配置的主人")
        if action.kind != "proactive_private" and not self._private_source_exists(action):
            return Receipt("failed", detail="原始私信不存在、已撤回或来源不匹配")
        # No owner exception, no relationship cache. This is the last remote read.
        if not self.allows_private_reply(action.account_id, action.recipient_id):
            return Receipt("failed", detail="当前不是互相关注关系，禁止回复私信")
        if before_write is not None and not before_write():
            return Receipt("failed", detail="私信发送前最终检查未通过")
        fields = {"msg[sender_uid]": action.account_id, "msg[receiver_id]": action.recipient_id,
                  "msg[receiver_type]": 1, "msg[msg_type]": 1, "msg[msg_status]": 0,
                  "msg[dev_id]": device, "msg[timestamp]": int(time.time()), "msg[new_face_version]": 1,
                  "msg[content]": encoded, "csrf": self.credentials["bili_jct"],
                  "csrf_token": self.credentials["bili_jct"], "build": 0, "mobi_app": "web"}
        try:
            response = self.client.post(VC_API + "/web_im/v1/web_im/send_msg", params=params, data=fields,
                                        headers={"Referer": "https://message.bilibili.com/"})
            if response.status_code != 200:
                return Receipt("unknown", detail=f"私信写请求 HTTP {response.status_code}，禁止自动重发", pause=response.status_code in {403, 412, 429})
            payload = response.json()
            if not isinstance(payload, dict) or "code" not in payload:
                raise ValueError()
        except (httpx.HTTPError, ValueError):
            return Receipt("unknown", detail="私信发送超时或响应不可解析，禁止自动重发")
        code = payload["code"]
        if code != 0:
            raise failure(f"私信未受理，接口错误 {code}", code=str(code),
                          pause=code in {-101, -111, -403, -412, -509, -352, -401, 12015, 21015, 21020, 21046})
        key = str((payload.get("data") or {}).get("msg_key") or "")
        if not number(key):
            return Receipt("unknown", detail="私信返回成功但没有可核对的消息 ID，禁止重发")
        return Receipt("accepted", key, "平台已受理私信")
