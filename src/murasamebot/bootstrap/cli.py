from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import tempfile
from threading import Event as StopEvent
import time

import httpx

from murasamebot.adapters.characters.lingchat import load_persona
from murasamebot.adapters.demo import run_demo
from murasamebot.bootstrap.config import Config
from murasamebot.bootstrap.runtime import build, prepare_store, readiness
from murasamebot.domain.models import Event, PlatformError


def show(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def worker_loop(config_path: Path, stop: StopEvent, *, once=False):
    import os
    prepare_store(Config(config_path)).audit("worker_started",f"工作循环启动 pid={os.getpid()}",time.time())
    if once:
        runtime = build(Config(config_path))
        try:
            runtime.tick()
            show(readiness(runtime))
        finally:
            runtime.close()
        return
    from murasamebot.bootstrap.workers import WorkerCoordinator
    coordinator = WorkerCoordinator(config_path)
    next_poll = 0.0
    try:
        while not stop.is_set():
            config = Config(config_path)
            if time.monotonic() >= next_poll:
                started = time.monotonic()
                runtime = build(config)
                try:
                    if not runtime.model.configured or not any(module.adapter.configured for module in runtime.modules.values()):
                        raise ValueError("请先填写本地模型配置并运行 login 完成扫码；可先运行 doctor 或 demo")
                    if runtime.store.acquire_lease("worker_tick", time.time(), 1200):
                        try:
                            runtime.poll()
                            runtime.poll_private()
                            runtime.store.set_setting("last_tick", time.time())
                        finally:
                            runtime.store.release_lease("worker_tick")
                finally:
                    runtime.close()
                next_poll = max(time.monotonic() + 1, started + min((getattr(module, "poll_seconds", 15) for module in runtime.modules.values()), default=15))
                coordinator.start_background()
            coordinator.pump(config)
            stop.wait(1)
    finally:
        coordinator.close()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="MurasameBot：跨平台社交内核与 B 站首版")
    result.add_argument("--config", type=Path, default=Path(__file__).resolve().parents[3] / "config.yml")
    sub = result.add_subparsers(dest="command", required=True)
    doctor = sub.add_parser("doctor", help="本地配置自检；--online 额外只读检查 B 站登录")
    doctor.add_argument("--online", action="store_true")
    sub.add_parser("demo", help="不联网的跨平台场景演示")
    run = sub.add_parser("run", help="启动工作循环")
    run.add_argument("--once", action="store_true")
    serve = sub.add_parser("serve", help="打开本机管理台服务")
    serve.add_argument("--with-worker", action="store_true", help="同时启动已配置的工作循环")
    sub.add_parser("login", help="扫码登录；凭证只写入本地 .secrets")
    sub.add_parser("qq-check", help="只读核验QQ机器人账号和主人好友关系，不发送消息")
    sub.add_parser("qq-listen", help="启动本机OneBot签名事件回调接收器")
    sub.add_parser("categories", help="显示适配器分区参考 ID")
    sub.add_parser("config", help="显示不含密钥的实际配置")
    check = sub.add_parser("comment-check", help="只读核查已受理评论的状态，不发布或重发")
    check.add_argument("id", help="管理台中的本地动作 ID")
    proxy = sub.add_parser("proxy-rules", help="查看网址的代理路由；不连接目标网址或打开账号数据库")
    proxy.add_argument("--refresh", action="store_true", help="下载并验证最新 GFWList 规则")
    proxy.add_argument("urls", nargs="*", help="需要判断的 HTTP(S) 网址")
    sub.add_parser("pause", help="暂停任务与出站")
    sub.add_parser("resume", help="人工解除暂停；认证错误仍会重新暂停")
    inspect = sub.add_parser("inspect", help="查看本地状态")
    inspect.add_argument("table", choices=["bindings", "events", "memories", "tasks", "actions", "views", "audit", "model_usage", "model_daily", "conversation_records", "memory_products", "memory_jobs", "memory_daily"])
    ingest = sub.add_parser("ingest", help="从本地 JSON 导入统一事件；用于接入测试")
    ingest.add_argument("file", type=Path)
    ingest.add_argument("--process", action="store_true", help="立即调用已配置模型处理；可能计费，但不执行发送")
    for command, label in [("approve", "批准草稿入发送队列"), ("reject", "取消待发送动作"), ("cancel", "取消委托"), ("forget", "删除记忆并取消关联未完成委托")]:
        entry = sub.add_parser(command, help=label)
        entry.add_argument("id")
    resolve = sub.add_parser("resolve", help="人工核对发送结果未知的动作，不重发")
    resolve.add_argument("id")
    resolve.add_argument("--state", choices=["accepted", "failed"], required=True)
    resolve.add_argument("--external-id", default="")
    bind = sub.add_parser("bind", help="由操作者核验后绑定自己的稳定平台身份")
    bind.add_argument("platform")
    bind.add_argument("external_id")
    bind.add_argument("--name", default="")
    revoke = sub.add_parser("revoke", help="撤销主人在指定平台的绑定")
    revoke.add_argument("platform")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Some platform write URLs contain CSRF parameters. Keep transport URLs out
    # of normal console logs; product diagnostics live in the scoped audit log.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        config = Config(args.config)
        if args.command == "qq-check":
            from murasamebot.adapters.platforms.qq import QQAdapter
            adapter = QQAdapter(config.data["qq"],prepare_store(config),config.data["owner_person_id"])
            try:
                binding = adapter.store.binding(config.data["owner_person_id"],"qq")
                show({"account_matches":bool(adapter.account()),"owner_is_friend":bool(binding and adapter.allows_private_reply(adapter.account_id,binding["external_id"])),"sent":False})
            finally:
                adapter.close()
            return
        if args.command == "qq-listen":
            from murasamebot.bootstrap.qq_receiver import serve
            serve(config.path)
            return
        if args.command == "proxy-rules":
            from murasamebot.adapters.network import rules_for
            policy = config.network_proxy
            if args.refresh:
                if not policy.proxy_url:
                    raise ValueError("请先开启代理，再下载规则")
                try:
                    show(rules_for(policy).refresh())
                except (OSError, ValueError, httpx.HTTPError):
                    raise ValueError("代理规则更新失败，旧缓存保留；请检查代理和规则来源") from None
            routes = []
            for url in args.urls:
                address = httpx.URL(url)
                if address.scheme not in {"http", "https"} or not address.host or address.userinfo:
                    raise ValueError("请输入无内嵌凭证的 HTTP(S) 网址")
                routes.append({"host": address.host, "route": "proxy" if policy.should_proxy(address) else "direct"})
            show({"enabled": bool(policy.proxy_url), "mode": policy.mode,
                  "rules": rules_for(policy).status(), "routes": routes})
            return
        if args.command == "demo":
            persona = load_persona(config.file("character_source"), config.file("character_profile"))
            with tempfile.TemporaryDirectory(prefix="murasame-demo-", dir=config.root) as folder:
                show(run_demo(Path(folder), persona))
            return
        if args.command == "config":
            show(config.public())
            return
        if args.command == "categories":
            from murasamebot.adapters.platforms.bilibili import REGIONS
            show({"catalog": REGIONS, "note": "社区接口兼容分区参考表；实际可用性需真实接口验收，网站导航可能不同。"})
            return
        if args.command == "serve":
            from murasamebot.bootstrap.web import serve
            serve(config.path, with_worker=args.with_worker)
            return
        if args.command == "run":
            worker_loop(config.path, StopEvent(), once=args.once)
            return
        store = prepare_store(config)
        if args.command in {"pause", "resume"}:
            paused = args.command == "pause"
            store.set_setting("paused", paused)
            if not paused:
                for platform in config.enabled_platforms:
                    store.set_setting(f"platform_paused:{platform}", False)
            store.audit("platform_resumed" if not paused else "platform_paused","命令行人工操作",time.time())
            show({"paused": paused})
            return
        if args.command == "bind":
            store.bind(config.data["owner_person_id"], args.platform, args.external_id, args.name)
            show({"bound": args.platform, "external_id": args.external_id})
            return
        if args.command == "revoke":
            store.revoke_binding(config.data["owner_person_id"], args.platform)
            show({"revoked": args.platform})
            return
        if args.command == "inspect":
            show(store.inspect(args.table))
            return
        if args.command in {"approve", "reject", "cancel", "forget", "resolve"}:
            if args.command == "approve":
                store.approve_action(args.id, time.time())
            elif args.command == "reject":
                store.reject_action(args.id)
            elif args.command == "cancel":
                store.cancel_task(args.id)
            elif args.command == "forget":
                store.forget(args.id)
            else:
                store.resolve_action(args.id, args.state, args.external_id, time.time())
            show({"operation": args.command, "id": args.id, "ok": True})
            return
        runtime = build(config)
        try:
            if args.command == "doctor":
                report = readiness(runtime)
                if args.online:
                    checks = {name: module.adapter.account() for name, module in runtime.modules.items()}
                    # Keep the original Bilibili-only doctor response shape.
                    report["account_check"] = checks["bilibili"] if set(checks) == {"bilibili"} else checks
                    report["note"] = "仅验证只读登录状态，未发布评论或验收其他接口"
                show(report)
            elif args.command == "comment-check":
                from dataclasses import asdict
                from murasamebot.application.publication import PublicationService
                show(asdict(PublicationService(runtime.store, {name: module.adapter for name, module in runtime.modules.items()}).check(args.id)))
            elif args.command == "ingest":
                data = json.loads(args.file.read_text(encoding="utf-8"))
                added = runtime.engine.ingest(Event.from_dict(data))
                processed = runtime.engine.process_one_event() if args.process else False
                show({"added": added, "processed_one_event": processed, "sent": False})
            elif args.command == "login":
                platform = runtime.require_platform("bilibili")
                login = platform.generate_login()
                path = config.root / ".secrets" / "login-qr.png"
                path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    import qrcode
                    qrcode.make(login["url"]).save(path)
                    print(f"请用 B 站手机 App 扫描本地二维码：{path}")
                except ImportError:
                    print("未安装二维码图片依赖，可安装 .[login] 后重试。扫码地址：" + login["url"])
                deadline = time.monotonic() + 180
                previous = None
                while time.monotonic() < deadline:
                    state = platform.poll_login(login["key"])
                    if state != previous:
                        print({"waiting": "等待扫码", "scanned": "请在手机确认", "expired": "二维码过期", "success": "登录成功", "error": "扫码失败"}[state])
                        previous = state
                    if state in {"success", "expired", "error"}:
                        if state == "success":
                            show(platform.account())
                        break
                    time.sleep(2)
        finally:
            runtime.close()
    except KeyboardInterrupt:
        print("已停止。任务与发送记录保存在本地数据库。")
    except (ValueError, OSError, KeyError, PlatformError) as exc:
        # Keep raw HTTP request objects and credential values out of tracebacks.
        print(f"无法执行：{exc}")
        raise SystemExit(2) from None
