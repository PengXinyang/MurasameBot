import ast
from dataclasses import replace
from datetime import datetime
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from murasamebot.adapters.characters.lingchat import load_persona
from murasamebot.application.scheduling import next_window
from murasamebot.bootstrap.config import Config
from murasamebot.bootstrap.runtime import prepare_store


ROOT = Path(__file__).resolve().parents[1]


def test_shared_modules_do_not_import_adapters_or_third_party_sdks():
    prohibited = {"httpx", "yaml", "sqlite3", "openai", "bilibili_api", "fastapi", "requests"}
    for directory in ("domain", "application", "ports"):
        for path in (ROOT / "src" / "murasamebot" / directory).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                modules = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
                assert not any(module.split(".")[0] in prohibited or module.startswith("murasamebot.adapters") or module.startswith("murasamebot.bootstrap") for module in modules), path
    code = f"import sys; sys.path.insert(0, {str(ROOT / 'src')!r}); import murasamebot.application.engine; import murasamebot.domain.models"
    subprocess.run([sys.executable, "-S", "-c", code], check=True, capture_output=True)


def test_persona_load_does_not_import_private_source_prompt_or_require_images(tmp_path):
    source = tmp_path / "settings.yml"
    source.write_text("ai_name: 丛雨\nuser_name: 私人姓名\nsystem_prompt: 不能公开的关系\nvoice_models: {}\n", encoding="utf-8")
    profile = tmp_path / "text.yml"
    profile.write_text("public_background: 你是丛雨\nstyle: 自称吾辈\nowner_style: 称已验证的人为主人\nexamples: []\n", encoding="utf-8")
    persona = load_persona(source, profile)
    assert "私人姓名" not in persona.prompt(True)
    assert "不能公开" not in persona.prompt(True)
    assert "主人" not in persona.prompt(False)


def test_cross_midnight_window_uses_starting_day():
    now = datetime.fromisoformat("2026-10-27T01:00:00+08:00").timestamp()
    assert next_window(now, "Asia/Hong_Kong", (("23:00", "02:00"),), (0,)) == now
    end = datetime.fromisoformat("2026-10-27T02:00:00+08:00").timestamp()
    assert next_window(end, "Asia/Hong_Kong", (("23:00", "02:00"),), (0,)) > end


def write_config(tmp_path):
    data = yaml.safe_load((ROOT / "config.yml").read_text(encoding="utf-8"))
    data["bindings"][0]["external_id"] = "424242"
    data["network"]["proxy_enabled"] = False
    data["character_source"] = str(ROOT / "character" / "settings.yml")
    data["character_profile"] = str(ROOT / "character" / "text_profile.yml")
    path = tmp_path / "config.yml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


def test_revoked_binding_is_not_resurrected_on_restart(tmp_path):
    path = write_config(tmp_path)
    store = prepare_store(Config(path))
    store.revoke_binding("owner", "bilibili")
    restarted = prepare_store(Config(path))
    assert restarted.binding("owner", "bilibili") is None


def test_public_config_masks_model_secret_and_rejects_nonlocal_dashboard(tmp_path, monkeypatch):
    path = write_config(tmp_path)
    monkeypatch.setenv("MURASAME_MODEL_API_KEY", "secret-value")
    config = Config(path)
    assert config.public()["model"]["api_key"] == "已配置"
    assert "secret-value" not in str(config.public())
    data = config.raw
    data["web"]["host"] = "0.0.0.0"
    with pytest.raises(ValueError):
        Config.validate(data)
