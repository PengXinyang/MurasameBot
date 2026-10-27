"""Render the local dashboard with an isolated browser profile and no real credentials."""
from http.server import ThreadingHTTPServer
from pathlib import Path
import json
import shutil
import subprocess
import sys
from threading import Thread
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import yaml
from murasamebot.bootstrap.web import handler_for
from murasamebot.bootstrap.config import Config
from murasamebot.bootstrap.runtime import prepare_store
from murasamebot.domain.models import Action, Event


def main():
    review = (ROOT / ".pytest-tmp" / "dashboard-review").resolve()
    if not review.is_relative_to(ROOT):
        raise RuntimeError("浏览器测试资料必须位于项目内")
    review.mkdir(parents=True, exist_ok=True)
    (review / "config.local.yml").unlink(missing_ok=True)
    data = yaml.safe_load((ROOT / "config.yml").read_text(encoding="utf-8"))
    data["bindings"][0]["external_id"] = "424242"
    data["network"]["proxy_enabled"] = False
    data["character_source"] = str(ROOT / "character" / "settings.yml")
    data["character_profile"] = str(ROOT / "character" / "text_profile.yml")
    data["model"].update({"base_url": "", "name": "", "api_key": ""})
    data["database"] = "data/preview-" + uuid4().hex[:10] + ".sqlite3"
    path = review / "config.yml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    preview_store = prepare_store(Config(path))
    stamp = time.time()
    preview_store.set_setting("inbox_warnings", {"notifications": [{"source": "notifications", "detail": "预览：通知超过 3 页，本轮保留最新 90 条；较旧消息未覆盖。"}]})
    preview_event = Event("preview-private", "bilibili", "999", "424242", "bilibili:999:private:424242", "private_message",
                          "今天有点累，想聊一会儿。", stamp, visibility="private", reply_to="100", sequence=1)
    preview_store.enqueue_event(preview_event)
    preview_store.finish_event(preview_event.event_id, "done")
    records=preview_store.memory_records("owner",f"conversation:bilibili:{preview_event.conversation_id}","private_dialogue")
    preview_store.memory_commit(records,[{"kind":"summary","text":"主人今天有点累，想有人陪着聊一会儿；这是一次聊天，不是找视频委托。","sources":[records[0]["record_id"]]},
        {"kind":"reflection","text":"今天的陪伴适合慢一点，先认真听主人说话。","sources":[records[0]["record_id"]]}],"diary",0,stamp)
    preview_store.memory_job("preview-review","diary","done",stamp)
    preview_store.set_setting("memory_health",{"state":"ready","at":stamp})
    preview_store.enqueue_action(Action("preview-private", data["agent_id"], "bilibili", "999", None,
                                       "主人辛苦啦，先歇一会儿，吾辈陪你慢慢聊。", stamp, stamp + 3600,
                                       kind="private_reply", evidence_level="conversation", visibility="private", recipient_id="424242", source_sequence=1,
                                       conversation_id=preview_event.conversation_id, event_id=preview_event.event_id, reply_to="100",
                                       review_state="rejected", review_reason="演示：模型未通过，请人工复核。"), draft=True, now=stamp)
    output = ROOT.parent / "docs" / "dashboard-preview.png"
    output.parent.mkdir(exist_ok=True)
    browser = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
    if not browser.is_file():
        raise RuntimeError("未找到本机 Edge，不能执行可视化预览")
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(path, "preview-token"))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        node = shutil.which("node")
        if node:
            screens = [("overview", "browsing", 1440, 1260, output),
                       ("settings", "browsing", 1440, 1460, output.parent / "settings-preview.png"),
                       ("settings", "messages", 1440, 1850, output.parent / "message-settings-preview.png"),
                       ("settings", "expression", 1440, 1200, output.parent / "expression-settings-preview.png"),
                       ("actions", "browsing", 1440, 1000, output.parent / "model-review-preview.png"),
                       ("memories", "browsing", 1440, 1500, output.parent / "memory-preview.png"),
                       ("overview", "browsing", 390, 1280, output.parent / "mobile-preview.png"),
                       ("settings", "messages", 390, 1280, output.parent / "mobile-settings-preview.png"),
                       ("memories", "browsing", 390, 1280, output.parent / "mobile-memory-preview.png")]
            plan = {"browser": str(browser), "profile": str(review / ("cdp-" + uuid4().hex[:8])),
                    "url": f"http://127.0.0.1:{server.server_port}/#token=preview-token",
                    "report": str(output.parent / "dashboard-browser-checks.json"),
                    "screens": [{"tab": tab, "group": group, "width": width, "height": height, "output": str(target)}
                                for tab, group, width, height, target in screens]}
            plan_path = review / "render-plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            result=subprocess.run([node, str(ROOT / "scripts" / "render_dashboard.mjs"), str(plan_path)],
                           capture_output=True,text=True,encoding="utf-8",timeout=90,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
            if result.returncode:
                raise RuntimeError("浏览器检查失败："+(result.stderr or result.stdout)[-4000:])
            print(result.stdout.strip())
            return
        for tab, size, target in (("overview", "1440,1260", output),
                                  ("settings", "1440,1440", output.parent / "settings-preview.png"),
                                  ("settings&group=messages", "1440,1680", output.parent / "message-settings-preview.png"),
                                  ("settings&group=expression", "1440,1080", output.parent / "expression-settings-preview.png"),
                                  ("actions", "1440,1000", output.parent / "model-review-preview.png"),
                                  ("overview", "390,1280", output.parent / "mobile-preview.png")):
            process = subprocess.run([
                str(browser), "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                "--disable-background-networking", "--disable-component-update", "--disable-sync",
                "--user-data-dir=" + str(review / ("browser-profile-" + target.stem)), "--window-size=" + size,
                "--virtual-time-budget=5000", "--screenshot=" + str(target),
                f"http://127.0.0.1:{server.server_port}/#token=preview-token&tab={tab}",
            ], capture_output=True, timeout=50, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if not target.exists():
                raise RuntimeError(f"未生成截图，浏览器退出码 {process.returncode}")
            print(target)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


if __name__ == "__main__":
    main()
