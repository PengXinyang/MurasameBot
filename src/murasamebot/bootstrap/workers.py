"""Bounded jobs owned by a single coordinator; each job owns its HTTP clients."""
from concurrent.futures import ThreadPoolExecutor
import logging
from pathlib import Path
import time

from murasamebot.adapters.models.concurrency import ModelConcurrency
from murasamebot.bootstrap.config import Config
from murasamebot.bootstrap.runtime import build, prepare_store


LOG = logging.getLogger("murasame")


class WorkerCoordinator:
    def __init__(self, config_path: Path, *, runtime_builder=build, config_loader=Config):
        self.path, self.build = config_path, runtime_builder
        self.config_loader = config_loader
        self.pool = ThreadPoolExecutor(max_workers=35, thread_name_prefix="murasame")
        self.model_limit = ModelConcurrency(4)
        self.messages, self.background, self.sender, self.memory = [], None, None, None
        self.observer = None

    def job(self, kind):
        config = self.config_loader(self.path)
        runtime = self.build(config, concurrency=self.model_limit)
        try:
            if kind == "observation":
                if not runtime.owner_history:
                    return False
                history_monitor=getattr(runtime.owner_history.source,"monitor",None)
                if history_monitor and not history_monitor.allowed("owner_history"):
                    return False
                return runtime.owner_history.sync(time.time())
            if kind == "memory":
                return runtime.engine.memory_service.maintain()
            if kind == "message":
                processed = runtime.engine.process_one_event()
                reviewed = runtime.engine.review_one()
                return processed or reviewed
            if kind == "send":
                return runtime.engine.dispatch_one(pre_reviewed_only=True)
            if not runtime.store.acquire_lease("background_decision", time.time(), 1200):
                return False
            try:
                runtime.run_background(time.time())
            finally:
                runtime.store.release_lease("background_decision")
            return False
        finally:
            runtime.close()

    def collect(self, future):
        try:
            future.result()
        except Exception as exc:
            # Do not log provider URLs, private text or credentials.
            LOG.error("后台任务失败：%s", type(exc).__name__)

    def pump(self, config):
        limit = config.data.get("processing", {}).get("model_concurrency", 4)
        self.model_limit.resize(limit)
        remaining = []
        for future in self.messages:
            if future.done():
                self.collect(future)
            else:
                remaining.append(future)
        self.messages = remaining
        store = prepare_store(config)
        if self.observer is not None and self.observer.done():
            self.collect(self.observer)
            self.observer = None
        observation = store.owner_history_state(config.data["owner_person_id"])
        if (self.observer is None and "bilibili" in config.enabled_platforms and config.data["owner_history"]["enabled"] and
                time.time()-observation.get("last_attempt",0)>=config.data["owner_history"]["poll_seconds"]):
            self.observer = self.pool.submit(self.job,"observation")
        store.recover_idle_work(time.time())
        state = store.queue_status(review_strategy=config.data.get("review",{}).get("strategy","inline"),
                                   routes=tuple((name, "") for name in config.enabled_platforms))
        ready = state.get("ready_events",state["pending_events"])
        jobs = ready + (state["review_ready_actions"] if config.data["mode"] == "reviewed" else 0)
        if jobs:
            for _ in range(min(jobs,max(0, limit - len(self.messages)))):
                self.messages.append(self.pool.submit(self.job, "message"))
        if (self.sender is None or self.sender.done()) and state.get("ready_actions", state["queued_actions"]):
            if self.sender is not None:
                self.collect(self.sender)
            self.sender = self.pool.submit(self.job, "send")
        if self.memory is not None and self.memory.done():
            self.collect(self.memory)
            self.memory = None
        # Maintenance gets a single lane and yields to incoming generation/review.
        if (self.memory is None and not self.messages and not ready
                and not state["review_ready_actions"] and not (self.background and not self.background.done())):
            now = time.time()
            if now-store.get_setting("memory_pump_at",0)>=30:
                store.set_setting("memory_pump_at",now)
                self.memory = self.pool.submit(self.job,"memory")

    def start_background(self):
        if self.background is None or self.background.done():
            if self.background is not None:
                self.collect(self.background)
            self.background = self.pool.submit(self.job, "background")

    def close(self):
        self.pool.shutdown(wait=True, cancel_futures=True)
