"""Render the local dashboard with an isolated browser profile and no real credentials."""
from http.server import ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
from threading import Thread

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import yaml
from murasamebot.bootstrap.web import handler_for


def main():
    review = (ROOT / ".pytest-tmp" / "dashboard-review").resolve()
    if not review.is_relative_to(ROOT):
        raise RuntimeError("浏览器测试资料必须位于项目内")
    review.mkdir(parents=True, exist_ok=True)
    data = yaml.safe_load((ROOT / "config.yml").read_text(encoding="utf-8"))
    data["bindings"][0]["external_id"] = "424242"
    data["network"]["proxy_enabled"] = False
    data["character_source"] = str(ROOT / "character" / "settings.yml")
    data["character_profile"] = str(ROOT / "character" / "text_profile.yml")
    data["model"].update({"base_url": "", "name": "", "api_key": ""})
    path = review / "config.yml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    output = ROOT.parent / "docs" / "dashboard-preview.png"
    output.parent.mkdir(exist_ok=True)
    browser = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
    if not browser.is_file():
        raise RuntimeError("未找到本机 Edge，不能执行可视化预览")
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(path, "preview-token"))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for tab, size, target in (("overview", "1365,1000", output),
                                  ("settings", "1365,1900", output.parent / "settings-preview.png")):
            process = subprocess.run([
                str(browser), "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                "--disable-background-networking", "--disable-component-update", "--disable-sync",
                "--user-data-dir=" + str(review / ("browser-profile-" + tab)), "--window-size=" + size,
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
