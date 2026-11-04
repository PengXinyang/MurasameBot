"""Manual state reads bypass automation spacing, never identity or cooldowns."""
from dataclasses import replace
import time

import httpx
import pytest

from murasamebot.adapters.persistence.sqlite import SQLiteStore
from murasamebot.adapters.platforms.bilibili import BilibiliAdapter
from murasamebot.adapters.request_monitor import MonitoredTransport, RequestMonitor
from murasamebot.application.publication import PublicationService
from murasamebot.application.request_policy import RequestSettings
from murasamebot.domain.models import PlatformError, PolicyError
from test_bili_cli_compatibility import accepted_action, comment_action, nav
from test_bilibili_contracts import credentials
from test_request_control import Clock


def probe(tmp_path, store, clock, *, state=0, second_page=False, response_code=0, http_status=200):
    monitor = RequestMonitor(store, RequestSettings(), clock=clock, sleep=clock.sleep)
    calls = []

    def handle(request):
        calls.append(request.url.path)
        assert request.method == "GET"
        if request.url.path.endswith("/nav"):
            return nav()
        found = not second_page or request.url.params["pn"] == "2"
        return httpx.Response(http_status, json={"code": response_code, "data": {
            "page": {"count": 21},
            "replies": [{"rpid": 800 if found else 1, "oid": 42, "mid": 999, "state": state}],
        }})

    client = httpx.Client(transport=MonitoredTransport(httpx.MockTransport(handle), monitor))
    return BilibiliAdapter(credentials(tmp_path), client=client, monitor=monitor), calls


@pytest.mark.parametrize("state", [0, 17])
@pytest.mark.parametrize("second_page", [False, True])
def test_manual_check_is_immediate_even_with_automation_queue_full(factory, now, tmp_path, state, second_page):
    _, store, _, _ = factory()
    clock = Clock(now)
    reserved = now + 120
    store.set_setting("bili_read_slot", reserved)
    bot, calls = probe(tmp_path, store, clock, state=state, second_page=second_page)
    try:
        result = bot.check_publication(comment_action(), "800", manual=True)
        assert result.platform_state == state
        assert result.checked_pages == (2 if second_page else 1)
        assert len(calls) == 1 + result.checked_pages
        assert clock.waits == []
        assert store.get_setting("bili_read_slot") == reserved
        assert store.request_status(now, bot.monitor.settings)["last_minute"] == len(calls)
        # The manual scope ends after the query; other reads still obey the queue.
        with pytest.raises(PlatformError) as exc:
            bot.account()
        assert exc.value.code == "read_deferred"
    finally:
        bot.close()


def test_default_check_retains_automation_spacing(factory, now, tmp_path):
    _, store, _, _ = factory()
    clock = Clock(now)
    store.set_setting("bili_read_slot", now + 9)
    bot, calls = probe(tmp_path, store, clock)
    try:
        assert bot.check_publication(comment_action(), "800").platform_state == 0
        assert clock.waits == [9, 3] and len(calls) == 2
    finally:
        bot.close()


@pytest.mark.parametrize("channel,expected_calls", [("identity", 0), ("content", 1)])
def test_manual_check_preserves_existing_cooldowns(factory, now, tmp_path, channel, expected_calls):
    _, store, _, _ = factory()
    clock = Clock(now)
    store.set_setting("bili_backoff:" + channel, {"until": now + 60})
    bot, calls = probe(tmp_path, store, clock)
    try:
        with pytest.raises(PlatformError) as exc:
            bot.check_publication(comment_action(), "800", manual=True)
        assert exc.value.code == "read_deferred"
        assert len(calls) == expected_calls and not clock.waits
    finally:
        bot.close()


@pytest.mark.parametrize("response_code,http_status", [(-509, 200), (0, 429)])
def test_manual_limit_response_still_backs_off_automatic_reads(factory, now, tmp_path, response_code, http_status):
    _, store, _, _ = factory()
    clock = Clock(now)
    bot, calls = probe(tmp_path, store, clock, response_code=response_code, http_status=http_status)
    try:
        with pytest.raises(PlatformError):
            bot.check_publication(comment_action(), "800", manual=True)
        assert store.request_cooldown("content")["until"] > now
        with pytest.raises(PlatformError) as exc:
            bot.monitor.before(httpx.Request("GET", "https://api.bilibili.com/x/v2/reply/reply"))
        assert exc.value.code == "read_deferred" and len(calls) == 2
    finally:
        bot.close()


def test_manual_check_still_rejects_wrong_account(factory, now, tmp_path):
    _, store, _, _ = factory()
    clock = Clock(now)
    bot, calls = probe(tmp_path, store, clock)
    try:
        with pytest.raises(PlatformError) as exc:
            bot.check_publication(replace(comment_action(), account_id="other-bot"), "800", manual=True)
        assert exc.value.code == "wrong_account" and exc.value.pause
        assert len(calls) == 1 and not clock.waits
    finally:
        bot.close()


def test_manual_scope_is_limited_to_paths_and_current_thread(factory, now):
    from concurrent.futures import ThreadPoolExecutor

    _, store, _, _ = factory()
    clock = Clock(now)
    monitor = RequestMonitor(store, RequestSettings(), clock=clock, sleep=clock.sleep)
    store.set_setting("bili_read_slot", now + 120)
    nav_request = httpx.Request("GET", "https://api.bilibili.com/x/web-interface/nav")
    with monitor.manual_reads(("/x/web-interface/nav",)):
        monitor.before(nav_request)
        with pytest.raises(PlatformError):
            monitor.before(httpx.Request("GET", "https://api.bilibili.com/x/msgfeed/at"))
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(monitor.before, nav_request)
            with pytest.raises(PlatformError):
                future.result()
    assert not clock.waits and store.get_setting("bili_read_slot") == now + 120


def test_manual_check_keeps_same_action_lease_and_never_requeues(factory, now):
    _, store, platform = accepted_action(factory, now)
    holder = SQLiteStore(store.path, store.agent_id)
    assert holder.acquire_lease("publication_check:checked", time.time(), 120)
    service = PublicationService(store, {"bilibili": object()})
    with pytest.raises(PolicyError, match="正在核查"):
        service.check("checked", manual=True)
    assert len(platform.sent) == 1 and store.inspect("actions")[0]["state"] == "accepted"
