"""Managed lifetime with isolated config, signed loopback events and fake AI."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
from http.server import ThreadingHTTPServer
import json
import socket
import subprocess
import sys
from threading import Event, Thread
import time

import httpx
import pytest
import yaml

from murasamebot.bootstrap.config import Config
from murasamebot.bootstrap.receivers import ReceiverManager, ReceiverSpec
from murasamebot.bootstrap.runtime import prepare_store
from murasamebot.bootstrap.web import handler_for
from murasamebot.bootstrap.worker_lock import WorkerLock
from test_architecture_and_config import ROOT, write_config


def config_path(tmp_path):
    path = write_config(tmp_path)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    Config(path).local_path.write_text(yaml.safe_dump({
        'qq': {'robot_id': '456', 'access_token': 'private-test-token',
               'callback_secret': 'test-signature', 'callback_port': port},
        'bindings': [{'person_id': 'owner', 'platform': 'qq', 'external_id': '123', 'purpose': 'owner'}]
    }), encoding='utf-8')
    return path


class IdleWorker:
    def __init__(self):
        self.calls = []
        self.started = Event()

    def __call__(self, path, stop, *, config_loader):
        self.calls.append(config_loader)
        self.started.set()
        stop.wait(10)


def wait_until(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(.01)


def test_start_qq_alone_then_bili_concurrently_uses_one_worker(tmp_path):
    path, worker = config_path(tmp_path), IdleWorker()
    manager = ReceiverManager(path, worker_target=worker)
    try:
        assert not manager.status()['worker_running']
        # Bili is configured enabled, but must not start just because QQ starts.
        manager.control('qq', 'start')
        assert worker.started.wait(2)
        assert worker.calls[0](path).enabled_platforms == ('qq',)
        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(lambda _: manager.control('qq', 'start'), range(5)))
        assert len(worker.calls) == 1
        manager.control('bilibili', 'start')
        assert set(worker.calls[0](path).enabled_platforms) == {'qq', 'bilibili'}
        assert all(p['running'] for p in manager.status()['platforms'].values())
        manager.control('qq', 'stop')
        status = manager.status()
        assert not status['platforms']['qq']['listening']
        assert status['platforms']['bilibili']['running']
        assert worker.calls[0](path).enabled_platforms == ('bilibili',)
        manager.control('qq', 'start')
        assert len(worker.calls) == 1 and manager.status()['platforms']['qq']['listening']
    finally:
        manager.close()
    assert not manager.worker.is_alive()
    # Process exit preserves the startup selection, unlike a platform stop.
    assert 'qq' in Config(path).enabled_platforms
    assert manager.worker_config(path).enabled_platforms == ()


def test_managed_http_callback_commits_and_stop_preserves_buffer(tmp_path, monkeypatch):
    import murasamebot.bootstrap.qq_receiver as receiver
    from murasamebot.adapters.platforms.qq import QQAdapter
    def fake_adapter(settings, store, owner):
        def respond(request):
            assert request.url.path in {'/get_login_info', '/get_friend_list'}
            data = {'user_id':456} if request.url.path == '/get_login_info' else [{'user_id':123}]
            return httpx.Response(200, json={'status':'ok','retcode':0,'data':data})
        return QQAdapter(settings,store,owner,client=httpx.Client(
            base_url='http://qq.test',transport=httpx.MockTransport(respond)))
    monkeypatch.setattr(receiver, 'QQAdapter', fake_adapter)
    path = config_path(tmp_path)
    manager = ReceiverManager(path, worker_target=IdleWorker())
    manager.control('qq', 'start')
    body = json.dumps({'post_type':'message', 'message_type':'private', 'sub_type':'friend',
        'self_id':456, 'user_id':123, 'message_id':42,
        'message':[{'type':'text','data':{'text':'测试接收'}}]}).encode()
    signature = 'sha1=' + hmac.new(b'test-signature', body, hashlib.sha1).hexdigest()
    url = f'http://127.0.0.1:{Config(path).data["qq"]["callback_port"]}/onebot/qq'
    try:
        with httpx.Client(trust_env=False) as client:
            assert client.post(url, content=body).status_code == 401
            assert client.post(url, content=body, headers={'X-Signature':signature}).status_code == 200
            assert client.post(url, content=body, headers={'X-Signature':signature}).status_code == 200
        store = prepare_store(Config(path))
        with store.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM chat_inbox WHERE state='pending'").fetchone()[0] == 1
        manager.control('qq', 'stop')
        with pytest.raises((httpx.ConnectError, httpx.ConnectTimeout)):
            httpx.post(url, content=body, timeout=1, trust_env=False)
        with store.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM chat_inbox WHERE state='pending'").fetchone()[0] == 1
    finally:
        manager.close()


def test_busy_callback_port_is_reported_and_bili_keeps_running(tmp_path):
    path = config_path(tmp_path)
    manager = ReceiverManager(path, worker_target=IdleWorker())
    manager.control('bilibili', 'start')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', Config(path).data['qq']['callback_port']))
        sock.listen()
        try:
            with pytest.raises(ValueError, match='端口占用'):
                manager.control('qq', 'start')
            status = manager.status()['platforms']
            assert status['bilibili']['running']
            assert status['qq']['state'] == 'error' and not status['qq']['enabled']
            assert not status['qq']['listening']
        finally:
            manager.close()


def test_auto_start_failure_does_not_stop_other_platform(tmp_path):
    path = config_path(tmp_path)
    Config(path).save_local({'platforms': {'qq': {'enabled': True}}})
    def fail(_):
        raise OSError('never expose private-test-token')
    manager = ReceiverManager(path, worker_target=IdleWorker(), registry={
        'qq': ReceiverSpec('http', fail), 'bilibili': ReceiverSpec('polling')})
    try:
        manager.start_configured()
        assert manager.status()['platforms']['bilibili']['running']
        assert manager.status()['platforms']['qq']['state'] == 'error'
        assert 'private-test-token' not in json.dumps(manager.status())
    finally:
        manager.close()


def test_combined_config_failure_still_applies_other_platform_changes(tmp_path):
    path = config_path(tmp_path)
    def fail(_):
        raise OSError('private-test-token')
    manager = ReceiverManager(path, worker_target=IdleWorker(), registry={
        'qq':ReceiverSpec('http',fail), 'bilibili':ReceiverSpec('polling')})
    try:
        manager.control('bilibili','start')
        with pytest.raises(ValueError, match='接收服务启动失败'):
            manager.configure({'platforms':{'qq':{'enabled':True},'bilibili':{'enabled':False}}})
        status = manager.status()['platforms']
        assert not status['bilibili']['selected'] and not status['bilibili']['enabled']
        assert status['qq']['state'] == 'error'
    finally:
        manager.close()


def test_dashboard_api_auth_control_config_and_shutdown(tmp_path):
    path = config_path(tmp_path)
    worker = IdleWorker()
    manager = ReceiverManager(path, worker_target=worker)
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(path, 'admin-test', receivers=manager))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    headers = {'Authorization':'Bearer admin-test'}
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}', trust_env=False) as client:
            assert client.get('/api/receivers').status_code == 401
            assert client.post('/api/receivers', json={'platform':'qq','operation':'start'}).status_code == 401
            assert client.post('/api/receivers', json={'platform':'qq','operation':'start'},
                               headers={**headers,'Origin':'https://foreign.invalid'}).status_code == 401
            assert not manager.status()['worker_running']
            for data in ({'platform':'qq','operation':'restart'}, {'platform':[],'operation':'start'},
                         {'platform':'qq','operation':'start','token':'secret'}):
                assert client.post('/api/receivers', json=data, headers=headers).status_code == 400
            result = client.post('/api/receivers',json={'platform':'qq','operation':'start'},headers=headers)
            assert result.status_code == 200, result.text
            assert result.json()['platforms']['qq']['listening']
            assert client.post('/api/config',json={'platforms':{'qq':{'enabled':False}}},headers=headers).status_code == 200
            assert not client.get('/api/receivers',headers=headers).json()['platforms']['qq']['listening']
            assert client.get('/assets/receivers.js').status_code == 200
            response = client.post('/api/config',json={'platforms':{'qq':{'enabled':True}}},headers=headers)
            assert response.status_code == 200, response.text
            assert client.get('/api/receivers',headers=headers).json()['platforms']['qq']['listening']
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
        manager.close()


def test_worker_error_visible_and_restart_is_possible(tmp_path):
    path = config_path(tmp_path)
    def fail(*args, **kwargs):
        raise ValueError('private-test-token')
    manager = ReceiverManager(path, worker_target=fail)
    try:
        manager.control('bilibili', 'start')
        wait_until(lambda: not manager.worker.is_alive())
        assert manager.status()['platforms']['bilibili']['state'] == 'error'
        assert 'private-test-token' not in json.dumps(manager.status())
        worker = IdleWorker()
        manager.worker_target = worker
        manager.control('bilibili','start')
        assert worker.started.wait(2)
        assert manager.status()['platforms']['bilibili']['running']
    finally:
        manager.close()


def test_worker_lock_rejects_other_process_and_releases_after_close(tmp_path):
    path = config_path(tmp_path)
    code = ('import sys;sys.path.insert(0,sys.argv[1]);'
            'from murasamebot.bootstrap.config import Config;'
            'from murasamebot.bootstrap.worker_lock import WorkerLock;'
            'lock=WorkerLock(Config(sys.argv[2]));lock.__enter__();lock.__exit__()')
    with WorkerLock(Config(path)):
        result = subprocess.run([sys.executable,'-c',code,str(ROOT/'src'),str(path)],capture_output=True,timeout=10)
        assert result.returncode != 0
        assert 'ValueError' in result.stderr.decode('utf-8',errors='replace')
    result = subprocess.run([sys.executable,'-c',code,str(ROOT/'src'),str(path)],capture_output=True,timeout=10)
    assert result.returncode == 0, result.stderr


def test_stopping_platform_invalidates_existing_runtime_routes_and_final_guard(tmp_path, now):
    from murasamebot.bootstrap.platforms import PlatformModule
    from murasamebot.bootstrap.runtime import build
    from test_platform_modules import FakeQQ, action
    path = config_path(tmp_path)
    manager = ReceiverManager(path, worker_target=IdleWorker())
    manager.control('qq', 'start')
    adapter = FakeQQ()
    runtime = build(manager.worker_config(path), platform_factories={
        'qq':lambda c,s:PlatformModule('qq',adapter,frozenset({'reply'}))})
    try:
        assert runtime.engine.routing() == (('qq','qq-bot'),)
        assert runtime.engine.action_policy(action(now),now) == ''
        manager.control('qq','stop')
        assert runtime.engine.routing() == ()
        assert '未启用' in runtime.engine.action_policy(action(now),now)
        # Even unchanged enabled settings must not allow sends after manager exit.
        manager.control('qq','start')
        manager.close()
        assert 'qq' in Config(path).enabled_platforms
        assert '未启用' in runtime.engine.action_policy(action(now),now)
        assert adapter.sent == []
    finally:
        runtime.close()
        manager.close()


def test_coordinator_jobs_reload_manager_selection(tmp_path):
    from types import SimpleNamespace
    from murasamebot.bootstrap.workers import WorkerCoordinator
    path = config_path(tmp_path)
    manager = ReceiverManager(path, worker_target=IdleWorker())
    configs = []
    def build(config, **kwargs):
        configs.append(config.enabled_platforms)
        return SimpleNamespace(engine=SimpleNamespace(memory_service=SimpleNamespace(maintain=lambda:True)), close=lambda:None)
    coordinator = WorkerCoordinator(path, runtime_builder=build, config_loader=manager.worker_config)
    try:
        manager.control('qq','start')
        assert coordinator.job('memory')
        manager.control('bilibili','start')
        manager.control('qq','stop')
        assert coordinator.job('memory')
        assert configs == [('qq',), ('bilibili',)]
    finally:
        manager.close()
        coordinator.close()
