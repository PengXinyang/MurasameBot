"""AutoProxy URL rules with exception precedence and a shared last-good cache."""
from __future__ import annotations

import base64
import binascii
import logging
import os
from pathlib import Path
import re
import tempfile
from threading import Lock
import time

import httpx


LOG = logging.getLogger(__name__)
MAX_BYTES = 2 * 1024 * 1024


def _pattern(rule: str) -> re.Pattern:
    if rule.startswith("/"):
        # GFWList includes one regex exception without a closing slash.
        return re.compile(rule[1:-1] if rule.endswith("/") else rule[1:], re.IGNORECASE)
    if "$" in rule or "##" in rule or "#@#" in rule:
        raise ValueError("代理规则含不支持的过滤器选项")
    domain_anchor = rule.startswith("||")
    start = rule.startswith("|") and not domain_anchor
    rule = rule[2:] if domain_anchor else rule[1:] if start else rule
    end = rule.endswith("|")
    if end:
        rule = rule[:-1]
    pattern = "".join(".*" if ch == "*" else r"(?:[^A-Za-z0-9_.%-]|$)" if ch == "^" else re.escape(ch) for ch in rule)
    if domain_anchor:
        pattern = r"^[a-z][a-z0-9+.-]*://(?:[^/?#.:]+\.)*" + pattern
    elif start:
        pattern = "^" + pattern
    return re.compile(pattern + ("$" if end else ""), re.IGNORECASE)


class RuleSet:
    def __init__(self, content: bytes):
        if len(content) > MAX_BYTES:
            raise ValueError("代理规则文件超过大小限制")
        try:
            text = content.decode("utf-8-sig")
            if not text.lstrip().startswith("[AutoProxy"):
                text = base64.b64decode(b"".join(content.split()), validate=True).decode("utf-8-sig")
        except (ValueError, UnicodeError, binascii.Error):
            raise ValueError("代理规则不是有效的 AutoProxy 文本或 Base64") from None
        if not text.lstrip().startswith("[AutoProxy"):
            raise ValueError("代理规则缺少 AutoProxy 文件头")
        self.proxy = []
        self.direct = []
        self.count = 0
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith(("!", "[")):
                continue
            target = self.direct if line.startswith("@@") else self.proxy
            try:
                target.append(_pattern(line[2:] if line.startswith("@@") else line))
            except re.error:
                raise ValueError("代理规则包含无效正则表达式") from None
            self.count += 1
        if not self.proxy:
            raise ValueError("代理规则中没有代理条目")

    def matches(self, url: str) -> bool:
        return not any(p.search(url) for p in self.direct) and any(p.search(url) for p in self.proxy)


class RuleCache:
    @staticmethod
    def _file_stamp(stat):
        # Windows can preserve the same observed mtime across quick writes.
        # Include length and identity to detect changed or atomically replaced files.
        return stat.st_mtime_ns, stat.st_size, stat.st_ino

    def __init__(self, path: Path, source_url: str, proxy_url: str, refresh_hours: int):
        self.path, self.source_url, self.proxy_url = path, source_url, proxy_url
        self.refresh_seconds = refresh_hours * 3600
        self.rules = None
        self.stamp = None
        self.next_attempt = 0.0
        self.warning = ""
        self.lock = Lock()

    def _download(self) -> bytes:
        # Bootstrap exception: fetch this configured rule source through the
        # proxy before rules exist. No account cookies or provider credentials.
        with httpx.Client(proxy=self.proxy_url or None, trust_env=False, timeout=httpx.Timeout(15, connect=5)) as http:
            with http.stream("GET", self.source_url) as response:
                response.raise_for_status()
                chunks, total = [], 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > MAX_BYTES:
                        raise ValueError("代理规则下载超过大小限制")
                    chunks.append(chunk)
                return b"".join(chunks)

    def _refresh(self):
        content = self._download()
        rules = RuleSet(content)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        name = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.path.parent, delete=False) as temp:
                name = temp.name
                temp.write(content)
            os.replace(name, self.path)
        finally:
            if name and os.path.exists(name):
                os.unlink(name)
        self.rules, self.stamp = rules, self._file_stamp(self.path.stat())
        self.warning = ""
        self.next_attempt = 0

    def refresh(self):
        with self.lock:
            self._refresh()
            return {"rules": self.rules.count, "cache_file": str(self.path), "updated": True}

    def get(self) -> RuleSet:
        with self.lock:
            now = time.time()
            stat = self.path.stat() if self.path.exists() else None
            invalid = False
            if stat and self._file_stamp(stat) != self.stamp:
                try:
                    with self.path.open("rb") as source:
                        rules = RuleSet(source.read(MAX_BYTES + 1))
                    self.rules, self.stamp = rules, self._file_stamp(stat)
                except (OSError, ValueError):
                    invalid = True
            if (invalid or self.rules is None or not stat or now - stat.st_mtime >= self.refresh_seconds) and now >= self.next_attempt:
                try:
                    self._refresh()
                except (httpx.HTTPError, OSError, ValueError):
                    self.next_attempt = now + 600
                    self.warning = "代理规则更新失败；继续使用上次有效规则" if self.rules else "代理规则不可用；需要规则的请求暂时失败，B站等直连请求不受影响"
                    LOG.warning(self.warning)
            if self.rules is None:
                raise httpx.ConnectError("代理规则不可用，请检查 7890 代理并运行 proxy-rules --refresh")
            return self.rules

    def status(self):
        return {"cached": self.path.exists(), "loaded_rules": self.rules.count if self.rules else None,
                "warning": self.warning}
