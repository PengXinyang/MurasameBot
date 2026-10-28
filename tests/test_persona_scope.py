from dataclasses import replace
from hashlib import sha256

import pytest
import yaml

from murasamebot.adapters.characters.lingchat import load_persona
from murasamebot.domain.models import Event
from test_architecture_and_config import ROOT
from test_private_messages import chat_engine, private_event


def test_character_secrets_and_owner_relationship_are_scoped_separately():
    persona = load_persona(ROOT / "character/settings.yml", ROOT / "character/text_profile.yml")
    public = persona.prompt()
    owner_public = persona.prompt(True)
    other_private = persona.prompt(False, private=True)
    owner_private = persona.prompt(True, private=True)
    for text in [public, owner_public, other_private]:
        assert "彭斌帝" not in text
        assert "绫" not in text
        assert "肺炎" not in text
        assert "恋人" not in text
    assert "主人" not in public
    assert "主人" in owner_public
    assert all(word in owner_private for word in ["彭斌帝", "绫", "恋人", "不表示主人已经知道"])


@pytest.mark.parametrize("private_context", [123, ["秘密"], {"text": "秘密"}])
def test_importer_rejects_malformed_private_context(tmp_path, private_context):
    source = tmp_path / "settings.yml"
    source.write_text("ai_name: 丛雨", encoding="utf-8")
    profile = tmp_path / "profile.yml"
    data = {"public_background": "角色背景", "style": "中文", "owner_style": "主人", "examples": [],
            "private_owner_context": private_context}
    profile.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ValueError, match="主人私聊人设"):
        load_persona(source, profile)


def test_persona_edit_changes_version_without_rewriting_source(tmp_path):
    source = ROOT / "character/settings.yml"
    before = sha256(source.read_bytes()).hexdigest()
    profile = tmp_path / "profile.yml"
    data = yaml.safe_load((ROOT / "character/text_profile.yml").read_text(encoding="utf-8"))
    profile.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    first = load_persona(source, profile)
    data["private_owner_context"] += " 仅用作虚构角色互动。"
    profile.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    second = load_persona(source, profile)
    assert first.version != second.version
    assert not second.source_changed
    assert sha256(source.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("peer,private", [("424242", True), ("555", True), ("424242", False), ("555", False)])
def test_generation_and_independent_review_use_the_same_persona_scope(factory, now, peer, private):
    engine, store, model, platform, _ = chat_engine(factory, model_review=True)
    engine.persona = replace(engine.persona, private_owner_context="仅主人私聊可见的人设标记")
    if private:
        event = private_event(now, peer=peer, text="今天聊点什么？")
        operation = "private_chat"
    else:
        event = Event("public-persona", "bilibili", "robot", peer, "public-conversation", "mention",
                      "今天聊点什么？", now, visibility="public", content=platform.ref, reply_to="700")
        operation = "reply"
    base = model.decide
    def decide(op, payload):
        if op == "review_action":
            model.calls.append((op, payload))
            return {"decision": "approve", "reason": "回复合适"}
        return base(op, payload)
    model.decide = decide
    engine.ingest(event)
    assert engine.process_one_event(now)
    assert engine.review_one(now + 1)
    generated = next(payload for op, payload in model.calls if op == operation)
    reviewed = next(payload for op, payload in model.calls if op == "review_action")
    expected = private and peer == "424242"
    assert ("仅主人私聊可见的人设标记" in generated["persona"]) == expected
    assert ("仅主人私聊可见的人设标记" in reviewed["persona"]) == expected
