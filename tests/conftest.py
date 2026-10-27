from copy import deepcopy
from dataclasses import replace
from datetime import datetime

import pytest

from murasamebot.adapters.persistence.sqlite import SQLiteStore
from murasamebot.application.engine import SocialEngine
from murasamebot.application.settings import Behaviour
from murasamebot.domain.models import ContentRef, Document, Event, Persona, Receipt


NOW = datetime.fromisoformat("2026-10-26T19:30:00+08:00").timestamp()


class TestModel:
    __test__ = False

    def __init__(self):
        self.calls = []
        self.intent = {"kind": "request", "topic": "摄影入门", "sensitive": False, "interval_days": 0}
        self.evaluation = {"decision": "recommend", "text": "这期把曝光与构图讲得很清楚，吾辈觉得适合入门。", "reason": "符合主题"}
        self.reply = {"decision": "reply", "text": "吾辈觉得关键在于先理解曝光与构图的联系。"}

    def decide(self, operation, payload):
        self.calls.append((operation, deepcopy(payload)))
        return deepcopy({"extract_intent": self.intent, "evaluate_video": self.evaluation, "reply": self.reply}[operation])


class TestPlatform:
    __test__ = False

    def __init__(self):
        self.ref = ContentRef("bilibili", "video", "10001")
        self.documents = {}
        self.sent = []
        self.searches = []
        self.receipt = Receipt("accepted", "98765", "模拟受理")

    def read(self, ref, *, reply_to="", root="", context=None):
        return self.documents.get(ref.key, Document(ref, "摄影入门", "本节介绍曝光、构图与练习方法。", "transcript", "123", "188",
                                                  comments=({"id": "700", "root": "600", "text": "应该怎么练习？"},)))

    def search(self, query, category_ids, limit):
        self.searches.append((query, list(category_ids)))
        return [self.ref]

    def discover(self, category_ids, limit):
        return [self.ref]

    def send(self, action, mentions, *, before_write=None):
        if before_write and not before_write():
            return Receipt("failed", detail="发送前校验未通过")
        self.sent.append((action, mentions))
        return self.receipt


@pytest.fixture
def now():
    return NOW


@pytest.fixture
def factory(tmp_path):
    def make(**overrides):
        store = SQLiteStore(tmp_path / "state.sqlite3", overrides.pop("agent_id", "test-agent"))
        store.bind("owner", "wechat", "wx-owner")
        store.bind("owner", "bilibili", "424242")
        settings = replace(Behaviour(agent_id=store.agent_id, account_id="robot", draft=False, browsing_enabled=True,
                                     categories=("188",), min_send_gap_seconds=1, recommendation_gap_minutes=1), **overrides)
        model, platform = TestModel(), TestPlatform()
        persona = Persona("丛雨", "你是丛雨。", "自称吾辈，中文简短。", "对已验证主人称主人。", (), "v1")
        engine = SocialEngine(settings, store, model, persona, {"bilibili": platform}, {"bilibili": platform})
        return engine, store, model, platform
    return make


def owner_message(now, event_id="request", text="今晚帮我找摄影入门视频"):
    return Event(event_id, "wechat", "wx-robot", "wx-owner", "wechat:private:owner", "chat", text, now, "private")


def queue_recommendation(engine, store, now):
    engine.ingest(owner_message(now))
    assert engine.process_one_event(now)
    assert engine.run_due_task(now)
    return store.inspect("actions")[0]["action_id"]
