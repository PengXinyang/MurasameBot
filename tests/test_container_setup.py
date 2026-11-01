from pathlib import Path
import runpy

from murasamebot.bootstrap.config import Config


def test_container_initialization_is_valid_and_preserves_user_state(tmp_path):
    image = Path(__file__).resolve().parents[1]
    prepare = runpy.run_path(str(image / "deploy/container_entrypoint.py"))["prepare"]
    root = tmp_path / "workspace"
    prepare(root,image)
    config = Config(root / "config.yml")
    assert not config.enabled_platforms
    assert not config.data["qq_calls"]["enabled"]
    assert config.data["qq"]["api_url"] == "http://127.0.0.1:3000"
    assert config.file("database").is_relative_to(root)
    (root / "config.local.yml").write_text("# user custom configuration", encoding="utf-8")
    (root / "character/text_profile.yml").write_text("custom persona", encoding="utf-8")
    (root / ".secrets/sentinel").write_text("private", encoding="utf-8")
    prepare(root,image)
    assert (root / "config.local.yml").read_text() == "# user custom configuration"
    assert (root / "character/text_profile.yml").read_text() == "custom persona"
    assert (root / ".secrets/sentinel").read_text() == "private"


def test_container_listener_keeps_auth_handler_and_closes_on_sigterm(tmp_path, monkeypatch):
    import signal
    from murasamebot.bootstrap import web, receivers
    from test_architecture_and_config import write_config
    captured = {}
    previous = object()
    def set_signal(number, handler):
        captured["handler"] = handler
        return previous
    class Receiver:
        def __init__(self, path): pass
        def close(self): captured["receivers_closed"] = True
    class Server:
        def __init__(self, address, handler): captured["address"] = address
        def serve_forever(self, **kwargs): captured["handler"](signal.SIGTERM, None)
        def server_close(self): captured["server_closed"] = True
    monkeypatch.setenv("MURASAME_CONTAINER_BIND", "1")
    monkeypatch.setattr(signal,"signal",set_signal)
    monkeypatch.setattr(receivers,"ReceiverManager",Receiver)
    monkeypatch.setattr(web,"ThreadingHTTPServer",Server)
    web.serve(write_config(tmp_path))
    assert captured["address"] == ("0.0.0.0", 8766)
    assert captured["receivers_closed"] and captured["server_closed"]
    assert captured["handler"] is previous
