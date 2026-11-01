"""Initialize a persistent workspace once; never copy host credentials into images."""
from pathlib import Path
import os
import shutil
import sys


def prepare(root: Path, image: Path, seed: Path | None = None):
    root.mkdir(parents=True, exist_ok=True)
    if seed is not None:
        if not seed.is_dir():
            raise ValueError("Container seed directory does not exist")
        # Copy only explicit initial settings/assets and AI login files.
        # Never import databases, owner grants or overwrite live volume state.
        names = ("config.yml", "config.local.yml", "character/settings.yml", "character/text_profile.yml",
                 "character/avatar/dashboard-smile.jpg", "character/avatar/dashboard-letter.jpg",
                 "character/avatar/dashboard-kimono.png", ".secrets/bilibili.json", ".secrets/bilibili.device.json",
                 "data/gfwlist.txt")
        for name in names:
            source, target = seed / name, root / name
            if source.is_file() and not target.exists():
                if name == "data/gfwlist.txt":
                    from murasamebot.adapters.proxy_rules import RuleSet, MAX_BYTES
                    try:
                        with source.open("rb") as handle:
                            content = handle.read(MAX_BYTES + 1)
                        RuleSet(content)
                    except (OSError, ValueError):
                        print("Invalid optional seed GFWList ignored; routing still requires valid rules", file=sys.stderr)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                shutil.copyfile(source, target)
                if name == "config.local.yml" or name.startswith(".secrets/"):
                    target.chmod(0o600)
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
    seed = os.environ.get("MURASAME_SEED_DIR", "")
    prepare(root, image, Path(seed) if seed else None)
    os.chdir(root)
    os.execv(sys.executable, [sys.executable, str(image / "run.py"), "--config", str(root / "config.yml"), *sys.argv[1:]])
