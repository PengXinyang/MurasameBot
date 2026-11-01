"""Initialize a persistent workspace once; never copy host credentials into images."""
from pathlib import Path
import os
import shutil
import sys


def prepare(root: Path, image: Path):
    root.mkdir(parents=True, exist_ok=True)
    for name in ("config.yml", "character/settings.yml", "character/text_profile.yml"):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copyfile(image / name, target)
    for name in ("dashboard-smile.jpg", "dashboard-letter.jpg", "dashboard-kimono.png"):
        source, target = image / "character/avatar" / name, root / "character/avatar" / name
        if source.exists() and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    local = root / "config.local.yml"
    if not local.exists():
        local.write_text("""# Container-only overrides. Edit locally; never commit credentials.
platforms:
  bilibili: {enabled: false}
  qq: {enabled: false}
network:
  proxy_enabled: false
  proxy_url: http://host.docker.internal:7890
qq:
  api_url: http://127.0.0.1:3000
  robot_id: ''
  access_token: ''
  callback_secret: ''
qq_calls:
  enabled: false
""", encoding="utf-8")
        local.chmod(0o600)
    for name in ("data", ".secrets"):
        (root / name).mkdir(exist_ok=True)
    (root / ".secrets").chmod(0o700)


if __name__ == "__main__":
    root, image = Path("/workspace"), Path("/opt/murasame")
    os.umask(0o077)
    prepare(root, image)
    os.chdir(root)
    os.execv(sys.executable, [sys.executable, str(image / "run.py"), "--config", str(root / "config.yml"), *sys.argv[1:]])
