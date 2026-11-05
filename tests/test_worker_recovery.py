"""Worker restart and slow-read regressions, with isolated state and fake I/O."""
from threading import Event
from types import SimpleNamespace
import time

import pytest

from murasamebot.bootstrap import cli
from murasamebot.bootstrap.config import Config
from murasamebot.bootstrap.receivers import ReceiverManager
from murasamebot.bootstrap.runtime import prepare_store
from murasamebot.bootstrap.worker_lock import WorkerConflictError, WorkerLock
from murasamebot.bootstrap.workers import WorkerCoordinator
from murasamebot.domain.models import Action, Receipt, stable_id
from test_architecture_and_config import write_config
from test_receivers import config_path, IdleWorker, wait_until
from test_private_messages import chat_engine, private_event


def test_exclusive_worker_start_recovers_only_orphaned_coordinator_read_leases(tmp_path, monkeypatch):
    path = write_config(tmp_path)
    store = prepare_store(Config(path))
    reset = ('worker_tick', 'background_decision', 'worker_poll:qq', 'worker_poll:bilibili')
    keep = ('memory_maintenance', 'owner_history_sync', 'publication:check', 'workerXpoll:keep', 'send')
    for name in reset + keep:
        assert store.acquire_lease(name, time.time(), 1200)
    store.set_setting('platform_paused:qq', True)
    store.set_setting('request_backoff:bot', {'until': 12345})
    def loop(*args, **kwargs):
        with store.connection() as db:
            names = {r[0] for r in db.execute('SELECT name FROM leases WHERE agent_id=?', (store.agent_id,))}
        assert names == set(keep)
        assert store.get_setting('platform_paused:qq') is True
        assert store.get_setting('request_backoff:bot') == {'until': 12345}
    monkeypatch.setattr(cli, '_worker_loop', loop)
    cli.worker_loop(path, Event())
    assert any(r['kind'] == 'worker_read_leases_recovered' for r in store.inspect('audit'))


def test_conflicting_worker_never_clears_existing_leases(tmp_path, monkeypatch):
    path = write_config(tmp_path)
    store = prepare_store(Config(path))
    assert store.acquire_lease('worker_tick', time.time(), 1200)
    monkeypatch.setattr(cli, '_worker_loop', lambda *a, **kw: pytest.fail('must not start'))
    with WorkerLock(Config(path)):
        with pytest.raises(WorkerConflictError):
            cli.worker_loop(path, Event())
    with store.connection() as db:
        assert db.execute('SELECT 1 FROM leases WHERE name=?', ('worker_tick',)).fetchone()


@pytest.mark.parametrize('state', ['accepted', 'unknown', 'sending'])
def test_read_lease_recovery_never_requeues_completed_or_ambiguous_writes(tmp_path, monkeypatch, state):
    path=write_config(tmp_path)
    store=prepare_store(Config(path))
    now=time.time()
    action=Action('old-write',store.agent_id,'bilibili','robot',None,'合成正文',now,now+3600)
    assert store.enqueue_action(action,draft=False,now=now)
    assert store.claim_action(now,100,100,now-86400,0,0)
    if state!='sending':
        store.finish_action(action.action_id,Receipt(state,'fake-receipt'),now)
    assert store.acquire_lease('worker_tick',now,1200)
    monkeypatch.setattr(cli,'_worker_loop',lambda *a,**kw:None)
    cli.worker_loop(path,Event())
    assert store.inspect('actions')[0]['state']==state


def test_slow_platform_poll_does_not_block_other_transport_generation_or_dispatch(factory, tmp_path):
    engine, store, model, sent, _ = chat_engine(factory)
    path = write_config(tmp_path)
    config = Config(path)
    config.data['platforms']['qq'] = {'enabled': True}
    config.data['agent_id'], config.data['database'] = store.agent_id, str(store.path)
    # Keep fixture bindings independent of Config's preparatory bookkeeping.
    store.set_setting('binding_config_stamp', stable_id(config.data['bindings']))
    blocked, release, qq_flushed = Event(), Event(), Event()
    count = {'bilibili': 0, 'qq': 0}
    selections = []
    modules = {name: SimpleNamespace(poll_seconds=1, adapter=SimpleNamespace(configured=True)) for name in config.enabled_platforms}
    def poll(*, platforms):
        name = platforms[0]
        count[name] += 1
        if name == 'bilibili':
            blocked.set()
            assert release.wait(10)
    def private(*, platforms):
        if platforms == ('qq',):
            qq_flushed.set()
    def build(current, **kwargs):
        selections.append(current.enabled_platforms)
        return SimpleNamespace(modules=modules, model=SimpleNamespace(configured=True), engine=engine,
            store=store, poll=poll, poll_private=private, close=lambda:None, owner_history=None)
    coordinator = WorkerCoordinator(path, runtime_builder=build, config_loader=lambda _:config)
    try:
        coordinator.poll(config)
        assert blocked.wait(2) and qq_flushed.wait(2)
        for _ in range(4):
            coordinator.poll(config)
        assert count == {'bilibili': 1, 'qq': 1}  # No duplicate read job while blocked.
        ev = private_event(time.time())
        store.set_setting('private_intent:'+ev.event_id, {'kind':'none'})
        engine.ingest(ev)
        coordinator.pump(config)
        wait_until(lambda: store.inspect('actions'))
        coordinator.pump(config)
        wait_until(lambda: store.inspect('actions')[0]['state'] == 'accepted')
        assert sent.sent
        assert not release.is_set()
        assert all(set(selection) == {'bilibili', 'qq'} for selection in selections)
        assert time.time()-store.get_setting('worker_heartbeat') < 5
    finally:
        release.set()
        coordinator.close()


def test_worker_status_detects_stalled_progress_and_recovers_without_losing_controls(tmp_path, monkeypatch):
    path = config_path(tmp_path)
    manager = ReceiverManager(path, worker_target=IdleWorker())
    try:
        manager.control('qq', 'start')
        assert manager.worker.is_alive()
        store = prepare_store(Config(path))
        manager.worker_started_at = time.time()-60
        store.set_setting('worker_heartbeat', time.time()-45)
        state = manager.status()
        assert state['worker_running'] and state['worker_stalled']
        assert state['platforms']['qq']['state'] == 'stalled'
        assert state['platforms']['qq']['listening']
        store.set_setting('worker_heartbeat', time.time())
        state = manager.status()
        assert not state['worker_stalled'] and state['platforms']['qq']['state'] == 'running'
        manager.control('qq','stop')
        assert not manager.status()['platforms']['qq']['listening']
    finally:
        manager.close()


@pytest.mark.parametrize('error,word', [(WorkerConflictError('secret'), '冲突'), (ValueError('secret'), '配置')])
def test_startup_errors_distinguish_lock_conflicts_and_configuration(tmp_path, error, word):
    path = config_path(tmp_path)
    def fail(*args, **kwargs):
        raise error
    manager = ReceiverManager(path, worker_target=fail)
    try:
        manager.control('bilibili','start')
        wait_until(lambda:not manager.worker.is_alive())
        message = manager.status()['worker_error']
        assert word in message and 'secret' not in message
        if word == '配置':
            assert '冲突' not in message
    finally:
        manager.close()
