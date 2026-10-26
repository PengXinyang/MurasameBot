from __future__ import annotations

from copy import deepcopy
from dataclasses import fields
from datetime import time as daytime
import os
from pathlib import Path
import re
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import yaml

from murasamebot.application.settings import Behaviour


def merge(base: dict, override: dict) -> dict:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key.strip()):
            raise ValueError(".env 包含无效字段")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


class Config:
    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        self.root = self.path.parent
        self.local_path = self.root / "config.local.yml"
        load_dotenv(self.root / ".env")
        raw = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("配置必须为 YAML 对象")
        override = yaml.safe_load(self.local_path.read_text(encoding="utf-8")) if self.local_path.exists() else {}
        if override and not isinstance(override, dict):
            raise ValueError("本地覆盖配置必须为对象")
        self.raw = merge(raw, override or {})
        self.validate(self.raw)

        def expand(value):
            if isinstance(value, str):
                return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", lambda m: os.environ.get(m[1], ""), value)
            if isinstance(value, dict):
                return {k: expand(v) for k, v in value.items()}
            if isinstance(value, list):
                return [expand(v) for v in value]
            return value
        self.data = expand(self.raw)
        # Empty optional template bindings are not identities. Validate expanded
        # values too so environment substitution cannot bypass URL validation.
        self.data["bindings"] = [b for b in self.data.get("bindings", []) if str(b.get("external_id", "")).strip()]
        self.validate(self.data)

    @staticmethod
    def validate(data: dict):
        if data.get("mode") not in {"draft", "auto"}:
            raise ValueError("mode 必须为 draft 或 auto")
        if data.get("text_language", "zh") != "zh":
            raise ValueError("首版所有文字输出固定为中文；日语仅为未来语音预留")
        ZoneInfo(data.get("timezone", "Asia/Hong_Kong"))
        browse = data.get("browsing", {})
        if type(browse.get("enabled")) is not bool or type(data.get("paused", False)) is not bool:
            raise ValueError("开关必须为 true 或 false")
        for window in browse.get("windows", []):
            daytime.fromisoformat(str(window["start"]))
            daytime.fromisoformat(str(window["end"]))
        weekdays = browse.get("weekdays", [])
        if not weekdays or any(type(day) is not int or day not in range(7) for day in weekdays):
            raise ValueError("weekdays 必须包含 0 到 6 的星期编号")
        if not browse.get("windows"):
            raise ValueError("至少配置一个浏览时段")
        for category in browse.get("categories", []):
            if not str(category).isdigit():
                raise ValueError("分区 ID 必须为数字")
        if not isinstance(browse.get("categories"), list) or len(browse["categories"]) > 20:
            raise ValueError("分区应为最多 20 项的列表")
        for field in ("interests", "owner_interests"):
            values = browse.get(field)
            if not isinstance(values, list) or len(values) > 30 or any(not isinstance(v, str) or len(v) > 120 for v in values):
                raise ValueError("兴趣需要为简短文字列表")
        for group in (data.get("limits", {}), {key: browse[key] for key in ("interval_minutes", "videos_per_round", "candidate_limit") if key in browse}):
            if any(type(value) is not int or value < 1 or value > 10000 for value in group.values()):
                raise ValueError("额度和间隔需要为合理的正整数")
        if browse.get("videos_per_round", 0) > 10 or browse.get("candidate_limit", 0) > 30:
            raise ValueError("每轮分析最多 10 个视频，候选最多 30 个")
        if type(data.get("bilibili", {}).get("poll_seconds")) is not int or data["bilibili"]["poll_seconds"] < 30:
            raise ValueError("通知轮询间隔至少为 30 秒")
        if data.get("web", {}).get("host", "127.0.0.1") not in {"127.0.0.1", "localhost"}:
            raise ValueError("管理台仅支持绑定本机地址")
        for binding in data.get("bindings", []):
            if not all(str(binding.get(key, "")).strip() for key in ("person_id", "platform", "external_id")):
                raise ValueError("账号绑定字段不完整")
        base = data.get("model", {}).get("base_url", "")
        if base and not base.startswith("${"):
            url = urlparse(base)
            if url.scheme not in {"http", "https"} or not url.hostname or url.username:
                raise ValueError("模型地址需要为无内嵌凭证的 http(s) 地址")
        network = data.get("network", {})
        if type(network.get("proxy_enabled", False)) is not bool:
            raise ValueError("代理开关必须为 true 或 false")
        proxy = network.get("proxy_url", "")
        if proxy and not proxy.startswith("${"):
            address = urlparse(proxy)
            if address.scheme not in {"http", "https", "socks5", "socks5h"} or not address.hostname or not address.port:
                raise ValueError("代理地址需要包含 http/https/socks5/socks5h 协议、主机和端口")
        if network.get("proxy_enabled") and not proxy:
            raise ValueError("开启代理时需要配置代理地址")
        for field in ("repost_owner_videos", "repost_owner_dynamics"):
            if type(data.get("bilibili", {}).get(field, False)) is not bool:
                raise ValueError("转发开关必须为 true 或 false")
        art = data.get("artwork", {})
        if type(art.get("enabled", False)) is not bool or art.get("delivery", "image") not in {"image", "link", "local"}:
            raise ValueError("美图开关或投递方式无效")
        if art:
            if not art.get("times") or len(art["times"]) > 12:
                raise ValueError("请配置每天 1 至 12 个美图时刻")
            for moment in art["times"]:
                if not isinstance(moment, str) or not re.fullmatch(r"\d{2}:\d{2}", moment):
                    raise ValueError("美图时刻格式为 HH:MM")
                daytime.fromisoformat(moment)
            days = art.get("weekdays", [])
            if not days or any(type(d) is not int or d not in range(7) for d in days):
                raise ValueError("美图星期编号应为 0 至 6")
            for key, maximum in (("grace_minutes", 120), ("candidate_limit", 20), ("max_caption_characters", 250)):
                if type(art.get(key)) is not int or not 1 <= art[key] <= maximum:
                    raise ValueError(f"美图 {key} 配置超出范围")
            if not isinstance(art.get("keywords"), list) or not 1 <= len(art["keywords"]) <= 30 or any(not isinstance(k, str) or not k.strip() or len(k) > 120 for k in art["keywords"]):
                raise ValueError("美图关键词应为 1 至 30 项简短文字")
            if not isinstance(art.get("sources"), list) or not art["sources"] or set(art["sources"]) - {"pixiv", "x"}:
                raise ValueError("美图来源支持 pixiv 和 x")
            if not isinstance(art.get("permissions", []), list):
                raise ValueError("转载授权需为列表")
            for grant in art.get("permissions", []):
                if not isinstance(grant, dict) or grant.get("source") not in {"pixiv", "x"} or not str(grant.get("artist_id", "")).isdigit() or not str(grant.get("evidence", "")).strip():
                    raise ValueError("转载授权需要来源、画师稳定 ID 和授权依据 evidence")
                if "work_ids" in grant and (not isinstance(grant["work_ids"], list) or not grant["work_ids"] or any(not str(i).isdigit() for i in grant["work_ids"])):
                    raise ValueError("限定作品授权 work_ids 需为非空数字 ID 列表")
                if grant.get("expires_at"):
                    from datetime import datetime
                    if datetime.fromisoformat(str(grant["expires_at"])).tzinfo is None:
                        raise ValueError("授权到期时间需要包含时区")

    def file(self, key: str) -> Path:
        return (self.root / self.data[key]).resolve()

    def behaviour(self, account_id: str = "") -> Behaviour:
        browse, limits = self.data["browsing"], self.data["limits"]
        values = {key: limits[key] for key in limits if key in {f.name for f in fields(Behaviour)}}
        return Behaviour(agent_id=self.data["agent_id"], owner_person_id=self.data["owner_person_id"],
                         account_id=account_id, timezone=self.data["timezone"], draft=self.data["mode"] == "draft",
                         categories=tuple(str(value) for value in browse["categories"]),
                         windows=tuple((w["start"], w["end"]) for w in browse["windows"]),
                         weekdays=tuple(browse["weekdays"]), browsing_enabled=browse["enabled"],
                         interval_minutes=browse["interval_minutes"], videos_per_round=browse["videos_per_round"],
                         candidate_limit=browse["candidate_limit"], interests=tuple(browse["interests"]),
                         owner_interests=tuple(browse["owner_interests"]), **values)

    def public(self) -> dict:
        data = deepcopy(self.data)
        data["model"]["api_key"] = "已配置" if data["model"].get("api_key") else "未配置"
        for field in ("pixiv_session", "x_bearer_token"):
            if "artwork" in data:
                data["artwork"][field] = "已配置" if data["artwork"].get(field) else "未配置"
        if data.get("network", {}).get("proxy_url"):
            url = urlparse(data["network"]["proxy_url"])
            if url.username is not None:
                data["network"]["proxy_url"] = url._replace(netloc="***@" + url.netloc.rsplit("@", 1)[-1]).geturl()
        return data

    @property
    def proxy_url(self) -> str:
        network = self.data.get("network", {})
        return network.get("proxy_url", "") if network.get("proxy_enabled", False) else ""

    def save_local(self, patch: dict):
        # The local dashboard can change non-secret product controls only.
        patch = deepcopy(patch)
        if "proxy_url" in patch.get("network", {}) and patch["network"]["proxy_url"] == self.public().get("network", {}).get("proxy_url"):
            patch["network"]["proxy_url"] = self.raw.get("network", {}).get("proxy_url", "")
        allowed = {"mode", "paused", "timezone", "browsing", "limits", "network", "artwork", "bilibili"}
        if set(patch) - allowed:
            raise ValueError("该接口只允许修改运行设置")
        if set(patch.get("artwork", {})) - {"enabled", "delivery", "times", "weekdays", "grace_minutes", "candidate_limit", "max_caption_characters", "keywords", "sources"}:
            raise ValueError("来源密钥和转载授权请在本地配置文件中管理")
        if set(patch.get("bilibili", {})) - {"owner_new_videos", "owner_new_dynamics", "repost_owner_videos", "repost_owner_dynamics"}:
            raise ValueError("登录信息请在本地私密配置中管理")
        candidate = merge(self.raw, patch)
        self.validate(candidate)
        current = yaml.safe_load(self.local_path.read_text(encoding="utf-8")) if self.local_path.exists() else {}
        temp = self.local_path.with_suffix(".tmp")
        temp.write_text(yaml.safe_dump(merge(current or {}, patch), allow_unicode=True, sort_keys=False), encoding="utf-8")
        temp.replace(self.local_path)
