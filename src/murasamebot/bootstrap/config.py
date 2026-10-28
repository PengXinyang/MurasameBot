from __future__ import annotations

from copy import deepcopy
from dataclasses import fields, asdict
from datetime import time as daytime
import os
from pathlib import Path
import re
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import yaml

from murasamebot.application.settings import Behaviour
from murasamebot.application.memory import MemorySettings


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
        from murasamebot.adapters.network import DIRECT_DOMAINS, GFWLIST_URL
        raw = merge({"memory":asdict(MemorySettings()),"review":{"strategy":"inline"},
                     "limits":{"event_retry_seconds":60},
                     "network":{"proxy_mode":"gfwlist", "gfwlist_url":GFWLIST_URL,
                                "gfwlist_cache":"data/gfwlist.txt", "gfwlist_refresh_hours":6,
                                "direct_domains":list(DIRECT_DOMAINS)}},raw)
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
        review = data.get("review",{})
        if not isinstance(review,dict) or set(review)-{"strategy"} or not isinstance(review.get("strategy","inline"),str) or review.get("strategy","inline") not in {"inline","independent"}:
            raise ValueError("审核策略只能为 inline（生成时判断）或 independent（独立模型复核）")
        memory = data.get("memory", {})
        if not isinstance(memory,dict) or set(memory)-set(MemorySettings.__dataclass_fields__):
            raise ValueError("未知的记忆设置")
        if type(memory.get("enabled",True)) is not bool:
            raise ValueError("记忆开关必须为布尔值")
        ranges = {"recent_messages":(1,200),"recall_limit":(1,50),"context_characters":(1000,36000),
                  "summary_messages":(2,200),"batch_messages":(2,500),"input_characters":(2000,36000),
                  "daily_call_limit":(0,1000),"retry_minutes":(1,1440)}
        for key,(low,high) in ranges.items():
            value = memory.get(key,getattr(MemorySettings(),key))
            if type(value) is not int or not low<=value<=high:
                raise ValueError(f"记忆 {key} 需为 {low} 至 {high} 的整数")
        ratio = memory.get("pressure_ratio",0.75)
        if type(ratio) not in {int,float} or not 0.3<=ratio<=0.95:
            raise ValueError("记忆压缩触发比例需要为 0.3 至 0.95")
        review = memory.get("review_time","23:00")
        if not isinstance(review,str) or not re.fullmatch(r"\d{2}:\d{2}",review):
            raise ValueError("记忆回顾时间格式为 HH:MM")
        daytime.fromisoformat(review)
        if memory.get("summary_messages",40)>memory.get("batch_messages",80):
            raise ValueError("摘要批次上限不能小于摘要消息触发数")
        if memory.get("context_characters",12000)>data.get("model",{}).get("max_context_characters",18000)*2:
            raise ValueError("记忆上下文预算超过模型输入配置")
        if data.get("mode") not in {"draft", "auto", "reviewed"}:
            raise ValueError("mode 必须为 draft、auto 或 reviewed")
        if data.get("text_language", "zh") != "zh":
            raise ValueError("首版所有文字输出固定为中文；日语仅为未来语音预留")
        ZoneInfo(data.get("timezone", "Asia/Hong_Kong"))
        browse = data.get("browsing", {})
        if type(browse.get("allow_metadata_video", True)) is not bool:
            raise ValueError("标题和简介分析开关需要为布尔值")
        if any(type(browse.get(key, True)) is not bool for key in ("comment_videos", "reply_comments", "recommend_owner")):
            raise ValueError("主动评论、接话与推荐开关需要为布尔值")
        expressions = data.get("expressions", {})
        if type(expressions.get("enabled", True)) is not bool:
            raise ValueError("表情开关需要为布尔值")
        if type(expressions.get("kaomoji_enabled", True)) is not bool:
            raise ValueError("颜文字开关需要为布尔值")
        count = expressions.get("max_per_message", 2)
        if type(count) is not int or not 1 <= count <= 2:
            raise ValueError("每条消息表情上限需要为 1 或 2")
        cache = expressions.get("cache_minutes", 360)
        if type(cache) is not int or not 1 <= cache <= 1440:
            raise ValueError("表情列表缓存需要为 1 至 1440 分钟")
        keywords = expressions.get("preferred_keywords", ["千恋万花", "丛雨"])
        if not isinstance(keywords, list) or not 1 <= len(keywords) <= 10 or any(not isinstance(k,str) or not k.strip() or len(k)>80 for k in keywords):
            raise ValueError("表情优先关键词需要为 1 至 10 项简短文字")
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
            raise ValueError("主人作品轮询间隔至少为 30 秒")
        reply_interval = data.get("bilibili", {}).get("reply_poll_seconds", 15)
        concurrency = data.get("processing", {}).get("model_concurrency", 4)
        if type(concurrency) is not int or not 1 <= concurrency <= 32:
            raise ValueError("模型并发数需要为 1 至 32")
        batch_limit = data.get("bilibili", {}).get("inbox_batch_limit", 90)
        if type(batch_limit) is not int or not 1 <= batch_limit <= 90:
            raise ValueError("每轮收件上限需要为 1 至 90 条")
        for key in ("notification_pages", "private_session_pages", "private_message_pages"):
            pages = data.get("bilibili", {}).get(key, 3)
            if type(pages) is not int or not 1 <= pages <= 3:
                raise ValueError("收件翻页数需要为 1 至 3 页，每页 30 条")
        daily_calls = data.get("model", {}).get("daily_call_limit", 150)
        if type(daily_calls) is not int or not 1 <= daily_calls <= 1000000:
            raise ValueError("每日模型调用额度需要为 1 至 1000000")
        if type(reply_interval) is not int or not 10 <= reply_interval <= 300:
            raise ValueError("消息轮询间隔需要为 10 至 300 秒")
        if data.get("web", {}).get("host", "127.0.0.1") not in {"127.0.0.1", "localhost"}:
            raise ValueError("管理台仅支持绑定本机地址")
        for binding in data.get("bindings", []):
            if not all(str(binding.get(key, "")).strip() for key in ("person_id", "platform", "external_id")):
                raise ValueError("账号绑定字段不完整")
        base = data.get("model", {}).get("base_url", "")
        output_limit = data.get("model", {}).get("max_output_tokens")
        if output_limit is not None and (type(output_limit) is not int or output_limit < 1):
            raise ValueError("max_output_tokens 应为 null（使用服务默认值）或正整数；具体上限由模型服务决定")
        if base and not base.startswith("${"):
            url = urlparse(base)
            if url.scheme not in {"http", "https"} or not url.hostname or url.username:
                raise ValueError("模型地址需要为无内嵌凭证的 http(s) 地址")
        network = data.get("network", {})
        if not isinstance(network, dict) or set(network) - {"proxy_enabled", "proxy_url", "proxy_mode", "gfwlist_url", "gfwlist_cache", "gfwlist_refresh_hours", "direct_domains"}:
            raise ValueError("未知的网络设置")
        if network.get("proxy_mode", "gfwlist") not in {"gfwlist", "all"}:
            raise ValueError("代理模式需要为 gfwlist 或 all")
        hours = network.get("gfwlist_refresh_hours", 6)
        if type(hours) is not int or not 1 <= hours <= 168:
            raise ValueError("代理规则刷新间隔需要为 1 至 168 小时")
        source = urlparse(network.get("gfwlist_url", "https://raw.githubusercontent.com/gfwlist/gfwlist/master/gfwlist.txt"))
        if source.scheme != "https" or not source.hostname or source.username or source.password or source.fragment:
            raise ValueError("代理规则来源需要为无内嵌凭证的 HTTPS 地址")
        cache = network.get("gfwlist_cache", "data/gfwlist.txt")
        if not isinstance(cache, str) or not cache.strip():
            raise ValueError("代理规则缓存路径不能为空")
        domains = network.get("direct_domains", [])
        if not isinstance(domains, list) or len(domains) > 100 or any(not isinstance(d, str) or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", d) or ".." in d for d in domains):
            raise ValueError("直连域名需要为不带协议或路径的小写域名列表")
        if type(network.get("proxy_enabled", False)) is not bool:
            raise ValueError("代理开关必须为 true 或 false")
        proxy = network.get("proxy_url", "")
        if proxy and not proxy.startswith("${"):
            address = urlparse(proxy)
            if address.scheme not in {"http", "https", "socks5", "socks5h"} or not address.hostname or not address.port:
                raise ValueError("代理地址需要包含 http/https/socks5/socks5h 协议、主机和端口")
        if network.get("proxy_enabled") and not proxy:
            raise ValueError("开启代理时需要配置代理地址")
        for field in ("repost_owner_videos", "repost_owner_dynamics", "private_messages_enabled", "read_video_subtitles"):
            if type(data.get("bilibili", {}).get(field, False)) is not bool:
                raise ValueError("转发开关必须为 true 或 false")
        engagement = data.get("engagement", {})
        if not isinstance(engagement, dict) or set(engagement) - {"video_like", "video_coin", "dynamic_like"} or any(type(v) is not bool for v in engagement.values()):
            raise ValueError("互动配置只接受 video_like、video_coin、dynamic_like 布尔开关")
        vision = data.get("vision", {})
        if type(vision.get("enabled", True)) is not bool:
            raise ValueError("视觉开关必须为布尔值")
        for key, maximum in (("max_images", 9), ("max_image_bytes", 32 * 1024 * 1024),
                             ("max_total_bytes", 32 * 1024 * 1024), ("cache_minutes", 10080), ("failure_retry_minutes", 1440)):
            value = vision.get(key)
            if vision and (type(value) is not int or not 1 <= value <= maximum):
                raise ValueError(f"视觉 {key} 配置超出允许范围")
        if vision.get("detail", "auto") not in {"auto", "low", "high", "original"}:
            raise ValueError("视觉 detail 配置无效")
        hosts = vision.get("allowed_image_hosts", [])
        if vision and (not isinstance(hosts, list) or not hosts or any(not isinstance(h, str) or not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", h) for h in hosts)):
            raise ValueError("图片来源需要为明确的域名列表")
        vision_base = vision.get("base_url", "")
        if vision_base and not vision_base.startswith("${"):
            parsed = urlparse(vision_base)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
                raise ValueError("视觉服务地址需要为无内嵌凭证的 http(s) 地址")
        for field in ("private_session_pages", "private_message_pages"):
            value = data.get("bilibili", {}).get(field, 3)
            if type(value) is not int or not 1 <= value <= 20:
                raise ValueError("私信分页上限需要为 1 至 20")
        age = data.get("bilibili", {}).get("private_message_max_age_hours", 24)
        if type(age) is not int or not 1 <= age <= 168:
            raise ValueError("私信消息有效期需要为 1 至 168 小时")
        if data.get("limits", {}).get("max_private_characters", 400) > 500:
            raise ValueError("私信正文最多配置 500 字符")
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
                         model_review=self.data["mode"] == "reviewed",
                         review_strategy=self.data.get("review",{}).get("strategy","inline"),
                         categories=tuple(str(value) for value in browse["categories"]),
                         windows=tuple((w["start"], w["end"]) for w in browse["windows"]),
                         weekdays=tuple(browse["weekdays"]), browsing_enabled=browse["enabled"],
                         allow_metadata_video=browse.get("allow_metadata_video", True),
                         **{key: browse.get(key, True) for key in ("comment_videos", "reply_comments", "recommend_owner")},
                         **{key: self.data.get("engagement", {}).get(key, False) for key in ("video_like", "video_coin", "dynamic_like")},
                         interval_minutes=browse["interval_minutes"], videos_per_round=browse["videos_per_round"],
                         candidate_limit=browse["candidate_limit"], interests=tuple(browse["interests"]),
                         owner_interests=tuple(browse["owner_interests"]), **values)

    def public(self) -> dict:
        data = deepcopy(self.data)
        data["model"]["api_key"] = "已配置" if data["model"].get("api_key") else "未配置"
        if "vision" in data:
            data["vision"]["api_key"] = "已配置" if data["vision"].get("api_key") else "复用主模型服务或未配置"
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

    @property
    def network_proxy(self):
        from murasamebot.adapters.network import ProxyPolicy
        network = self.data["network"]
        return ProxyPolicy(proxy_url=self.proxy_url, mode=network["proxy_mode"],
                           source_url=network["gfwlist_url"], cache_file=(self.root / network["gfwlist_cache"]).resolve(),
                           refresh_hours=network["gfwlist_refresh_hours"], direct_domains=tuple(network["direct_domains"]))

    def save_local(self, patch: dict):
        # The local dashboard can change non-secret product controls only.
        patch = deepcopy(patch)
        if "proxy_url" in patch.get("network", {}) and patch["network"]["proxy_url"] == self.public().get("network", {}).get("proxy_url"):
            patch["network"]["proxy_url"] = self.raw.get("network", {}).get("proxy_url", "")
        allowed = {"mode", "paused", "timezone", "browsing", "limits", "network", "artwork", "bilibili", "vision", "engagement", "processing", "model", "expressions", "memory", "review"}
        if set(patch) - allowed:
            raise ValueError("该接口只允许修改运行设置")
        if set(patch.get("network", {})) - {"proxy_enabled", "proxy_url", "proxy_mode", "gfwlist_refresh_hours", "direct_domains"}:
            raise ValueError("规则来源和缓存路径请在本地配置文件中管理")
        if set(patch.get("artwork", {})) - {"enabled", "delivery", "times", "weekdays", "grace_minutes", "candidate_limit", "max_caption_characters", "keywords", "sources"}:
            raise ValueError("来源密钥和转载授权请在本地配置文件中管理")
        if set(patch.get("vision", {})) - {"enabled", "max_images", "detail"}:
            raise ValueError("视觉服务密钥与网络来源请在本地文件配置")
        if set(patch.get("model", {})) - {"daily_call_limit"}:
            raise ValueError("模型密钥、地址与模型名称请在本地文件配置")
        if set(patch.get("processing", {})) - {"model_concurrency"}:
            raise ValueError("未知的并发处理设置")
        if set(patch.get("expressions", {})) - {"enabled", "kaomoji_enabled", "max_per_message", "cache_minutes", "preferred_keywords"}:
            raise ValueError("未知的表情设置")
        if set(patch.get("bilibili", {})) - {"owner_new_videos", "owner_new_dynamics", "repost_owner_videos", "repost_owner_dynamics", "private_messages_enabled", "reply_poll_seconds", "poll_seconds", "read_video_subtitles", "inbox_batch_limit", "notification_pages", "private_session_pages", "private_message_pages"}:
            raise ValueError("登录信息请在本地私密配置中管理")
        candidate = merge(self.raw, patch)
        self.validate(candidate)
        current = yaml.safe_load(self.local_path.read_text(encoding="utf-8")) if self.local_path.exists() else {}
        temp = self.local_path.with_suffix(".tmp")
        temp.write_text(yaml.safe_dump(merge(current or {}, patch), allow_unicode=True, sort_keys=False), encoding="utf-8")
        temp.replace(self.local_path)
