"""Bounded recovery of comments hidden behind grouped notifications."""
import json
from murasamebot.domain.models import ContentRef, Event, PlatformError, stable_id


def expand_grouped(bot, item, uid, now):
    source = item.get("item") or {}
    if source.get("business_id") != 1 or not (item.get("is_multi") or item.get("counts", 0) > 1):
        return []
    oid = str(source.get("subject_id") or "")
    if not oid.isdigit():
        return []
    key = stable_id(oid, source.get("root_id"), source.get("source_id"), item.get("counts"), item.get("reply_time"))
    checkpoints = getattr(bot, "grouped_checkpoints", {})
    if checkpoints.get(key, 0) > now:
        return []
    if getattr(bot, "grouped_pages_remaining", 3) <= 0 or getattr(bot, "grouped_groups_remaining", 3) <= 0:
        bot.warn_inbox("grouped", "本轮合并评论补拉预算已用完，后续轮询继续检查")
        return []
    bot.grouped_groups_remaining = getattr(bot, "grouped_groups_remaining", 3) - 1
    try:
        video, _ = bot._request("/x/web-interface/view", params={"aid": oid})
        if str((video.get("owner") or {}).get("mid")) != uid:
            checkpoints[key] = now + 3600
            return []
        root = str(source.get("root_id") or "0")
        rows, offset, complete = {}, "", False
        for page in range(1, min(bot.pages, 3, getattr(bot, "grouped_pages_remaining", 3)) + 1):
            bot.grouped_pages_remaining = getattr(bot, "grouped_pages_remaining", 3) - 1
            if root != "0":
                data, _ = bot._request("/x/v2/reply/reply", params={"oid": oid, "type": 1, "root": root, "pn": page, "ps": 30})
                batch = ([data["root"]] if isinstance(data.get("root"), dict) else []) + (data.get("replies") or [])
                info = data.get("page") or {}
                size = info.get("size") or 30
                complete = not data.get("replies") or page * size >= info.get("count", 10**9)
            else:
                data, _ = bot._request("/x/v2/reply/wbi/main", signed=True, params={"oid": oid, "type": 1, "mode": 2,
                    "ps": 30, "pagination_str": json.dumps({"offset": offset})})
                batch = (data.get("replies") or []) + (data.get("top_replies") or [])
                cursor = data.get("cursor") or {}
                next_offset = ((cursor.get("pagination_reply") or {}).get("next_offset") or "")
                complete = not batch or cursor.get("is_end") is True
            for row in batch:
                if not isinstance(row, dict):
                    continue
                rows[str(row.get("rpid_str") or row.get("rpid"))] = row
                for child in (row.get("replies") or [])[:30]:
                    if isinstance(child, dict):
                        rows[str(child.get("rpid_str") or child.get("rpid"))] = child
            if complete:
                break
            if root == "0":
                if not next_offset or next_offset == offset:
                    break
                offset = next_offset
        events = []
        for rpid, row in rows.items():
            member = row.get("member") or {}
            actor, parent = str(member.get("mid") or ""), str(row.get("parent") or "0")
            thread = str(row.get("root") or "0")
            content = row.get("content") or {}
            if (not rpid.isdigit() or not actor.isdigit() or actor == uid
                    or str(row.get("oid", oid)) != oid or str(row.get("type", 1)) != "1"):
                continue
            occurred = row.get("ctime")
            if type(occurred) not in (int, float) or not now - bot.max_age <= occurred <= now:
                continue
            parent_ai = str(((rows.get(parent) or {}).get("member") or {}).get("mid")) == uid
            mentioned = any(str(m.get("mid")) == uid for m in content.get("members") or [] if isinstance(m, dict))
            if thread != "0" and not parent_ai and not mentioned:
                continue
            thread = rpid if thread == "0" else thread
            events.append(Event(f"bilibili:{uid}:comment:1:{oid}:{rpid}", "bilibili", uid, actor,
                f"bilibili:{uid}:bilibili:video:{oid}:thread:{thread}", "reply", str(content.get("message") or ""),
                occurred, content=ContentRef("bilibili", "video", oid), reply_to=rpid, thread_root=thread,
                actor_name=str(member.get("uname") or "")))
        checkpoints[key] = now + (3600 if complete else 60)
        if not complete:
            bot.warn_inbox("grouped", "合并评论补拉达到分页边界或缺少游标，较旧评论可能未覆盖")
        return events
    except PlatformError as exc:
        if exc.pause:
            raise
        checkpoints[key] = now + 60
        bot.warn_inbox("grouped", "合并评论补拉未完成，将稍后重试；普通通知仍保留")
        return []
