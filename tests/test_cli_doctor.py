"""CLI account checks use fake adapters and never start services or send messages."""
import json
from types import SimpleNamespace

import httpx
import pytest

from murasamebot.bootstrap import cli
from murasamebot.domain.models import PlatformError


@pytest.fixture
def doctor(monkeypatch, capsys):
    def run(outcomes, *, online=True):
        calls = []

        def adapter(name, outcome):
            def account():
                calls.append(name)
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
            return SimpleNamespace(account=account)

        runtime = SimpleNamespace(
            modules={name: SimpleNamespace(adapter=adapter(name, result)) for name, result in outcomes.items()},
            close=lambda: calls.append("closed"),
        )
        monkeypatch.setattr(cli, "Config", lambda path: object())
        monkeypatch.setattr(cli, "prepare_store", lambda config: object())
        monkeypatch.setattr(cli, "build", lambda config: runtime)
        monkeypatch.setattr(cli, "readiness", lambda runtime: {"model_configured": True, "live_verified": False})
        code = 0
        try:
            cli.main(["doctor", *(["--online"] if online else [])])
        except SystemExit as exc:
            code = exc.code
        output = capsys.readouterr().out
        return code, json.loads(output), calls, output
    return run


@pytest.mark.parametrize("failed_first", [True, False])
def test_unavailable_qq_does_not_hide_bilibili_result(doctor, failed_first):
    failed = ("qq", PlatformError("private-response", code="qq_unavailable", platform="qq"))
    passed = ("bilibili", {"uid": "test-bot"})
    code, report, calls, output = doctor(dict([failed, passed] if failed_first else [passed, failed]))
    assert code == 2
    assert report["online_ok"] is False
    assert report["online_checks"]["qq"]["ok"] is False
    assert "NapCat" in report["online_checks"]["qq"]["message"]
    assert "qq.api_url" in report["online_checks"]["qq"]["message"]
    assert report["online_checks"]["bilibili"] == {"ok": True}
    assert report["account_check"] == {"qq": None, "bilibili": {"uid": "test-bot"}}
    assert set(calls[:-1]) == {"qq", "bilibili"} and calls[-1] == "closed"
    assert "private-response" not in output and "无法执行" not in output
    assert report["live_verified"] is False


def test_bilibili_only_preserves_legacy_account_shape(doctor):
    code, report, calls, _ = doctor({"bilibili": {"uid": "test-bot"}})
    assert code == 0 and report["online_ok"] is True
    assert report["account_check"] == {"uid": "test-bot"}
    assert calls == ["bilibili", "closed"]


def test_all_enabled_platforms_can_pass(doctor):
    code, report, calls, _ = doctor({"qq": {"uid": "qq-bot"}, "bilibili": {"uid": "bili-bot"}})
    assert code == 0 and report["online_ok"] is True
    assert report["account_check"]["qq"]["uid"] == "qq-bot"
    assert calls == ["qq", "bilibili", "closed"]


@pytest.mark.parametrize("failure", [
    httpx.ConnectError("private-token-and-url"),
    ValueError("private-token-and-url"),
    OSError("private-token-and-url"),
    PlatformError("private-token-and-url", code="private-token-and-url"),
])
def test_transport_and_unknown_errors_are_reported_without_raw_details(doctor, failure):
    code, report, calls, output = doctor({"qq": failure})
    assert code == 2 and report["online_ok"] is False
    assert report["online_checks"]["qq"]["error_code"] == "account_check_failed"
    assert "private-token-and-url" not in output
    assert calls == ["qq", "closed"]


def test_failure_of_both_platforms_keeps_both_diagnostics(doctor):
    code, report, _, _ = doctor({
        "qq": PlatformError("private", code="qq_wrong_account"),
        "bilibili": PlatformError("private", code="auth_expired"),
    })
    assert code == 2
    assert report["online_checks"]["qq"]["error_code"] == "qq_wrong_account"
    assert report["online_checks"]["bilibili"]["error_code"] == "auth_expired"


def test_no_enabled_platform_is_not_a_successful_online_check(doctor):
    code, report, calls, _ = doctor({})
    assert code == 2 and report["online_ok"] is False
    assert report["online_checks"] == {} and "未启用平台" in report["note"]
    assert calls == ["closed"]


def test_offline_doctor_does_not_call_platform_account(doctor):
    code, report, calls, _ = doctor({"qq": AssertionError("must not call")}, online=False)
    assert code == 0 and "online_checks" not in report
    assert calls == ["closed"]
