from hashlib import sha256
from pathlib import Path

import yaml

from murasamebot.domain.models import Persona


def load_persona(source: Path, profile_path: Path) -> Persona:
    """Read approved text fields only; raw source prompt never becomes a public prompt."""
    raw = source.read_bytes()
    settings = yaml.safe_load(raw.decode("utf-8"))
    profile_bytes = profile_path.read_bytes()
    profile = yaml.safe_load(profile_bytes.decode("utf-8"))
    if not isinstance(settings, dict) or not isinstance(profile, dict):
        raise ValueError("角色配置必须是 YAML 对象")
    required = ("public_background", "style", "owner_style", "examples")
    if any(key not in profile for key in required):
        raise ValueError("缺少经范围区分的文字人设字段")
    if not all(isinstance(profile[key], str) and profile[key].strip() for key in required[:3]):
        raise ValueError("文字人设字段不能为空")
    if not isinstance(profile["examples"], list) or not all(isinstance(s, str) for s in profile["examples"]):
        raise ValueError("角色示例必须是字符串列表")
    source_hash = sha256(raw).hexdigest()
    name = settings.get("ai_name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("源配置缺少角色名")
    return Persona(name, profile["public_background"], profile["style"], profile["owner_style"],
                   tuple(profile["examples"]), sha256(raw + profile_bytes).hexdigest(),
                   source_changed=profile.get("source_sha256") != source_hash)

