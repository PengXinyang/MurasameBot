"""Bounded, read-only comment ancestry. No sibling text enters a reactive prompt."""
from __future__ import annotations

import re

from murasamebot.domain.models import CommentContext, PlatformError


PAGE_SIZE = 20  # reply/reply returns at most 20 even with a larger ps.
MAX_PAGES = 3
MAX_DEPTH = 12
MAX_TEXT = 6000


def identifier(row, name):
    value = str(row.get(name + "_str") or row.get(name) or "")
    return value if re.fullmatch(r"\d{1,24}", value) else ""


def read_comment_chain(request, *, oid, type_, target, root, account_id):
    nodes, issues = {}, []
    requests = 0
    dialog_attempted = False

    def add(rows):
        # Some responses embed replies under root; bound nested traversal too.
        pending = list(rows)[:80]
        for _ in range(100):
            if not pending:
                break
            row = pending.pop(0)
            if not isinstance(row, dict):
                continue
            key = identifier(row, "rpid")
            row_root = identifier(row, "root")
            if (not key or (key == root and row_root not in {"", "0"})
                    or (key != root and row_root != root)
                    or ("oid" in row and str(row["oid"]) != oid)
                    or ("type" in row and str(row["type"]) != str(type_))):
                continue
            nodes.setdefault(key, row)
            children = row.get("replies")
            if isinstance(children, list):
                pending.extend(children[:20])

    def path():
        chain, seen, missing = [], set(), []
        current = target
        while current:
            if current in seen:
                return chain, missing, "cycle"
            if len(chain) >= MAX_DEPTH:
                return chain, [current], "depth_limit"
            seen.add(current)
            row = nodes.get(current)
            if row is None:
                return chain, [current], "missing_comment"
            chain.append(row)
            if current == root:
                return chain, missing, ""
            current = identifier(row, "parent")
            if current in {"", "0"}:
                return chain, missing, "missing_parent_link"
        return chain, missing, "missing_parent_link"

    def fetch(endpoint, params):
        nonlocal requests
        requests += 1
        try:
            data, _ = request(endpoint, params={"oid": oid, "type": type_, "root": root, **params})
        except PlatformError as exc:
            if exc.pause:
                raise
            issues.append("read_unavailable")
            return None
        add(([data["root"]] if isinstance(data.get("root"), dict) else [])
            + (data.get("replies") if isinstance(data.get("replies"), list) else []))
        return data

    for page in range(1, MAX_PAGES + 1):
        data = fetch("/x/v2/reply/reply", {"ps": PAGE_SIZE, "pn": page})
        if data is None:
            break
        _, _, problem = path()
        if not problem:
            break
        # Once the target is located, its platform dialog id can fill earlier
        # ancestors without traversing every unrelated reply in the floor.
        dialog = identifier(nodes.get(target, {}), "dialog")
        if dialog not in {"", "0"} and not dialog_attempted:
            dialog_attempted = True
            fetch("/x/v2/reply/dialog/cursor", {"dialog": dialog, "size": 20, "min_floor": 0})
            if not path()[2]:
                break
        rows = data.get("replies") or []
        count = (data.get("page") or {}).get("count")
        if (isinstance(count, int) and page * PAGE_SIZE >= count) or len(rows) < PAGE_SIZE:
            break
        if page == MAX_PAGES:
            issues.append("page_limit")

    chain, missing, problem = path()
    if problem:
        issues.append(problem)
    # Keep the root as an explicitly separate anchor if a middle link is missing.
    # Its presence does not imply that the missing parent directly replies to it.
    if root not in {identifier(row, "rpid") for row in chain}:
        if root in nodes:
            if len(chain) == MAX_DEPTH:
                chain.pop()
                issues.append("depth_limit")
            chain.append(nodes[root])
        elif root not in missing:
            missing.append(root)
            issues.append("missing_root")
    chain.reverse()
    comments = []
    text_limit = min(1500, MAX_TEXT // max(1, len(chain)))
    for row in chain:
        key = identifier(row, "rpid")
        member = row.get("member") or {}
        author = str(member.get("mid") or row.get("mid") or "")
        text = str((row.get("content") or {}).get("message") or "")
        truncated = len(text) > text_limit
        if truncated:
            issues.append("text_limit")
        comments.append({"id": key, "root": root, "parent": identifier(row, "parent"),
                         "author_id": author, "author_name": str(member.get("uname") or "")[:100],
                         "role": "agent" if author and author == account_id else "participant",
                         "ctime": str(row.get("ctime") or ""), "text": text[:text_limit],
                         "text_truncated": "true" if truncated else "false"})
    coverage = CommentContext(target, root, "partial" if issues else "complete",
                              tuple(dict.fromkeys(missing)), tuple(dict.fromkeys(issues)), requests)
    return tuple(comments), coverage
