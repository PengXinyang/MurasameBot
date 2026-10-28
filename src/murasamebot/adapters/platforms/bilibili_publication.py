"""Bounded authenticated comment checks; finding a comment is not public proof."""
from __future__ import annotations

import time

from murasamebot.domain.models import PublicationCheck, PolicyError


def classify_comment(reply: dict, *, pages: int = 0) -> PublicationCheck:
    state = reply.get("state")
    state = state if type(state) is int else None
    folder = reply.get("folder")
    folded = isinstance(folder, dict) and folder.get("is_folded") is True
    if state == 17:
        status, detail = "self_only", "平台状态17：疑似仅发送账号可见，未自动重发"
    elif state == 11:
        status, detail = "pending", "平台状态11：疑似审核中，可稍后只读复查"
    elif folded:
        status, detail = "folded", "平台返回折叠标记，公开展示可能受限"
    elif reply.get("invisible") is True or state not in {0, None}:
        status, detail = "abnormal", "平台返回异常可见性状态，不能视为公开展示"
    elif state == 0:
        if pages:
            status, detail = "account_visible", "登录账号视角状态正常；尚不能证明其他账号可见"
        else:
            status, detail = "unverified", "发布回执状态正常；尚未回查评论列表，公开可见性未确认"
    else:
        status, detail = "unverified", "平台未提供完整评论状态，可稍后核查"
    return PublicationCheck(status, detail, time.time(), state, pages)


class BilibiliPublicationMixin:
    def check_publication(self, action, external_id: str) -> PublicationCheck:
        # Local import avoids an adapter import cycle with the other mixins.
        from murasamebot.adapters.platforms.bilibili import COMMENT_TYPES, PlatformError, numeric
        if (not action.content or action.content.provider != "bilibili" or action.content.kind not in COMMENT_TYPES
                or action.kind not in {"reply", "owner_comment", "browse_comment", "browse_reply", "recommendation"}
                or action.visibility != "public"):
            raise PolicyError("该动作不是可核查的公开评论")
        if self.account()["uid"] != action.account_id:
            raise PlatformError("不能使用不同账号检查该评论", code="wrong_account", pause=True)
        target = numeric(external_id)
        root = numeric(action.thread_root or action.reply_to or target)
        for page in (1, 2):
            data, _ = self._request("/x/v2/reply/reply", params={"oid": numeric(action.content.external_id),
                "type": COMMENT_TYPES[action.content.kind], "root": root, "pn": page, "ps": 20})
            replies = data.get("replies") or []
            if not isinstance(replies, list):
                raise PlatformError("评论列表格式无效，本次未判定可见性")
            parent = data.get("root")
            rows = ([parent] if isinstance(parent, dict) else []) + replies
            for row in rows:
                if isinstance(row, dict) and str(row.get("rpid_str") or row.get("rpid")) == target:
                    # Never infer visibility from a different comment returned by
                    # a filtered list or from an accidental routing mismatch.
                    member = row.get("member") or {}
                    if (not isinstance(member, dict) or str(row.get("oid", "")) != action.content.external_id
                            or str(member.get("mid") or row.get("mid", "")) != action.account_id):
                        return PublicationCheck("unavailable", "评论响应的目标或作者不匹配，未判定可见性", time.time(), checked_pages=page)
                    return classify_comment(row, pages=page)
            page_info = data.get("page") or {}
            count = page_info.get("count") if isinstance(page_info, dict) else None
            if not replies or (type(count) is int and page * 20 >= count):
                break
        return PublicationCheck("not_found", "本次有限分页内未找到评论；可能待审、已删除或不在所查页中，未自动重发", time.time(), checked_pages=page)
