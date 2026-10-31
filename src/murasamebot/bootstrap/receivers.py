"""Process-owned event transports; all platforms share the existing coordinator."""
from dataclasses import dataclass
from threading import Event, RLock, Thread

from murasamebot.bootstrap.config import Config


class HTTPReceiver:
    def __init__(self, server):
        self.server = server
        self.thread = Thread(target=lambda: server.serve_forever(poll_interval=0.2),
                             name="murasame-qq-receiver", daemon=True)

    def start(self):
        try:
            self.thread.start()
        except BaseException:
            self.server.server_close()
            raise

    def is_alive(self):
        return self.thread.is_alive()

    def close(self):
        if self.thread.is_alive():
            self.server.shutdown()
            self.thread.join(timeout=2)
        self.server.server_close()


@dataclass(frozen=True)
class ReceiverSpec:
    mode: str
    factory: object = None


def default_receivers():
    def qq(path):
        from murasamebot.bootstrap.qq_receiver import create_server
        return HTTPReceiver(create_server(path))
    # Polling stays in PlatformModule.poll/poll_private, never another AI loop.
    return {"bilibili": ReceiverSpec("polling"), "qq": ReceiverSpec("http", qq)}


class ReceiverManager:
    def __init__(self, path, *, registry=None, worker_target=None):
        self.calls=None
        self.path = path
        self.registry = default_receivers() if registry is None else dict(registry)
        if worker_target is None:
            from murasamebot.bootstrap.cli import worker_loop
            worker_target = worker_loop
        self.worker_target = worker_target
        self.lock, self.stop = RLock(), Event()
        self.active, self.receivers, self.errors = set(), {}, {}
        self.worker, self.worker_error, self.closed = None, "", False

    def worker_config(self, path):
        config = Config(path)
        with self.lock:
            selected = set() if self.closed else set(self.active)
        for name, options in config.data["platforms"].items():
            options["enabled"] = options["enabled"] and name in selected
        config.runtime_reload = lambda: self.worker_config(path)
        return config

    def _work(self):
        try:
            self.worker_target(self.path, self.stop, config_loader=self.worker_config)
            if not self.stop.is_set():
                with self.lock:
                    self.worker_error = "工作循环已退出，请检查本地配置后重新启动"
        except Exception as exc:
            # Never expose arbitrary upstream exception strings/URLs/secrets.
            with self.lock:
                self.worker_error = ("工作循环未启动：可能已有独立worker，请先停止原进程并检查本地配置"
                                     if isinstance(exc, ValueError) else "工作循环异常退出，请检查本地配置后重新启动")

    def _start(self, name):
        if self.closed:
            raise ValueError("管理台正在关闭")
        spec = self.registry[name]
        if spec.factory and not (name in self.receivers and self.receivers[name].is_alive()):
            old = self.receivers.pop(name, None)
            if old:
                old.close()
            receiver = spec.factory(self.path)
            try:
                receiver.start()
            except BaseException:
                receiver.close()
                raise
            self.receivers[name] = receiver
        self.active.add(name)
        self.errors.pop(name, None)
        if self.worker is None or not self.worker.is_alive():
            self.worker_error = ""
            self.worker = Thread(target=self._work, name="murasame-worker", daemon=True)
            self.worker.start()

    def _stop(self, name):
        if name=="qq" and self.calls:self.calls.close()
        self.active.discard(name)
        receiver = self.receivers.pop(name, None)
        if receiver:
            receiver.close()
        self.errors.pop(name, None)

    def configure(self, patch):
        """Serialize settings and lifecycle changes made through this dashboard."""
        with self.lock:
            if self.closed:
                raise ValueError("管理台正在关闭")
            changes = patch.get("platforms", {})
            if not isinstance(changes, dict) or set(changes) - self.registry.keys():
                raise ValueError("未知的平台接收服务")
            config = Config(self.path)
            config.save_local(patch)
            failures = []
            for name, options in changes.items():
                if options["enabled"]:
                    try:
                        self._start(name)
                    except Exception:
                        self._stop(name)
                        Config(self.path).save_local({"platforms": {name: {"enabled": False}}})
                        self.errors[name] = "接收服务启动失败，请检查回调密钥及端口占用（先停止独立qq-listen）"
                        failures.append(self.errors[name])
                else:
                    self._stop(name)
            if failures:
                raise ValueError("；".join(failures)) from None

    def call_control(self,data):
        with self.lock:
            if self.calls is None:
                from murasamebot.bootstrap.qq_calls import CallManager
                self.calls=CallManager(self.path,self.worker_config)
            return self.calls.control(data)

    def control(self, name, operation):
        if name not in self.registry or operation not in {"start", "stop"}:
            raise ValueError("未知的平台或启停操作")
        self.configure({"platforms": {name: {"enabled": operation == "start"}}})
        return self.status()

    def start_configured(self):
        # Failure of one receiver must not prevent starting another platform.
        for name in Config(self.path).enabled_platforms:
            if name not in self.registry:
                self.errors[name] = "该平台尚未注册接收服务"
                continue
            try:
                with self.lock:
                    self._start(name)
            except Exception:
                with self.lock:
                    self.errors[name] = "接收服务启动失败，请检查回调密钥及端口占用"

    def status(self):
        config = Config(self.path)
        with self.lock:
            worker = bool(self.worker and self.worker.is_alive() and not self.stop.is_set())
            platforms = {}
            for name, spec in self.registry.items():
                enabled = name in config.enabled_platforms
                selected = name in self.active and not self.closed
                receiver = self.receivers.get(name)
                listening = bool(receiver and receiver.is_alive())
                running = enabled and selected and worker and (not spec.factory or listening)
                error = self.errors.get(name, "") or (self.worker_error if selected else "")
                if selected and spec.factory and not listening and not error:
                    error = "事件接收服务已退出，请重新启动"
                platforms[name] = {"enabled": enabled, "selected": selected, "running": running,
                    "mode": spec.mode, "listening": listening, "error": error,
                    "state": "error" if error else "running" if running else "stopped"}
            return {"managed": True, "worker_running": worker, "worker_error": self.worker_error,
                    "platforms": platforms}

    def close(self):
        if self.calls:self.calls.close()
        with self.lock:
            self.closed = True
            self.stop.set()
            self.active.clear()
            receivers, self.receivers = list(self.receivers.values()), {}
            worker = self.worker
        for receiver in receivers:
            receiver.close()
        if worker:
            worker.join(timeout=2)
