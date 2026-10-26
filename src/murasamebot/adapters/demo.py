"""Explicit offline scenario adapters. Never used as a fallback for a live model."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path

from murasamebot.adapters.persistence.sqlite import SQLiteStore
from murasamebot.application.engine import SocialEngine
from murasamebot.application.settings import Behaviour
from murasamebot.domain.models import ContentRef, Document, Event, Persona, Receipt


class ScenarioModel:
    def __init__(self):
        self.calls = []

    def decide(self, operation, payload):
        self.calls.append((operation, payload))
        if operation == "extract_intent":
            return {"kind": "request", "topic": "摄影入门", "sensitive": False, "not_before": None, "interval_days": 0}
        if operation == "evaluate_video":
            return {"decision": "recommend", "text": "这期把曝光和构图讲得很清楚，吾辈觉得挺适合入门。", "reason": "实际字幕符合摄影入门主题"}
        return {"decision": "reply", "text": "吾辈看这段说明，关键还是先把曝光和构图的关系弄清楚。", "reason": "回应原评论"}


class ScenarioPlatform:
    def __init__(self):
        self.sent = []
        self.searches = []
        self.ref = ContentRef("bilibili", "video", "10001")

    def read(self, ref, *, reply_to="", root=""):
        return Document(ref, "摄影入门：曝光与构图", "本节介绍光圈、快门、感光度与构图的联系。", "transcript", "123456", "188", "https://www.bilibili.com/video/demo")

    def search(self, query, category_ids, limit):
        self.searches.append((query, list(category_ids)))
        return [self.ref]

    def discover(self, category_ids, limit):
        return [self.ref]

    def send(self, action, mentions, *, before_write=None):
        if before_write and not before_write():
            return Receipt("failed", detail="模拟发送前校验未通过")
        self.sent.append((action, dict(mentions)))
        return Receipt("accepted", f"demo-{len(self.sent)}", "模拟回执，不代表真实平台发送")


def run_demo(directory: Path, persona: Persona) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore(directory / "scenario.sqlite3", "demo")
    store.bind("owner", "wechat", "owner-wechat")
    store.bind("owner", "bilibili", "424242")
    model, platform = ScenarioModel(), ScenarioPlatform()
    now = datetime.fromisoformat("2026-10-26T19:30:00+08:00").timestamp()
    settings = Behaviour(agent_id="demo", account_id="robot", draft=False, browsing_enabled=True,
                         min_send_gap_seconds=1, recommendation_gap_minutes=1)
    engine = SocialEngine(settings, store, model, persona, {"bilibili": platform}, {"bilibili": platform})
    event = Event("demo-request", "wechat", "wechat-robot", "owner-wechat", "wechat:private:owner", "chat",
                  "今晚想看摄影入门视频", now, "private")
    engine.ingest(event)
    engine.process_one_event(now)
    engine.run_due_task(now)
    engine.dispatch_one(now)
    # Construct a new store and engine to demonstrate restart persistence and deduplication.
    restarted = SQLiteStore(directory / "scenario.sqlite3", "demo")
    duplicate = restarted.enqueue_event(event)
    return {"mode": "offline_simulation", "real_network_requests": 0,
            "scenario": "微信请求 → 持久任务 → 找到视频 → B站 @ 主人（模拟）",
            "actions": [{"text": action.text, "mentions": mentions, "receipt": "模拟受理"} for action, mentions in platform.sent],
            "tasks": [{"topic": row["topic"], "state": row["state"]} for row in restarted.inspect("tasks")],
            "duplicate_event_accepted_after_restart": duplicate,
            "note": "这是固定场景模型与假平台，仅验证代码流程，不代表真实 AI 或平台接口已验收。"}
