"""Exercise Windows deployment control flow without Docker, browsers or accounts."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHELL = shutil.which("powershell.exe")
pytestmark = pytest.mark.skipif(not SHELL, reason="Windows PowerShell launcher")


def run_launcher(tmp_path, *, validate=False, engine="linux", build_fails=False, base_missing=False, pull_fails=False):
    for name in ("docker.env", "compose.local.yml", "workspace/config.local.yml", "napcat-config/onebot11_fake.json"):
        path=tmp_path / "deploy/local" / name
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text("{}",encoding="utf-8")
    trace=tmp_path / "trace.txt"
    script=tmp_path / "mock.ps1"
    quote=lambda p: "'"+str(p).replace("'","''")+"'"
    script.write_text(r'''
function global:docker {
    $global:LASTEXITCODE=0
    Add-Content -LiteralPath $env:MOCK_TRACE -Value ($args -join ' ')
    if ($args[0] -eq 'info') {
        if ($env:MOCK_ENGINE -eq 'warming') {
            $global:mockInfoCount++
            if ($global:mockInfoCount -eq 1) { throw 'Engine is not ready' }
            Write-Output 'linux'
        } else { Write-Output $env:MOCK_ENGINE }
    }
    elseif ($args -contains 'up') { if ($env:MOCK_BUILD_FAIL -eq '1') { $global:LASTEXITCODE=17 } }
    elseif ($args[0] -eq 'image') { if ($env:MOCK_BASE_MISSING -eq '1') { $global:LASTEXITCODE=1 } }
    elseif ($args[0] -eq 'pull') { if ($env:MOCK_PULL_FAIL -eq '1') { $global:LASTEXITCODE=1 } }
    elseif ($args -contains 'port') { Write-Output ('127.0.0.1:' + $args[-1]) }
    elseif ($args -contains 'exec') { Write-Output 'fake_management_token_1234567890' }
}
function global:Test-Path {
    param([string]$LiteralPath,[string]$PathType)
    if ($env:MOCK_ENGINE -eq 'warming' -and $LiteralPath -like '*Docker Desktop.exe') { return $true }
    Microsoft.PowerShell.Management\Test-Path -LiteralPath $LiteralPath -PathType $PathType
}
function global:Invoke-WebRequest { [pscustomobject]@{StatusCode=200} }
function global:Start-Process {
    param([string]$FilePath,[string]$WindowStyle)
    Add-Content -LiteralPath $env:MOCK_TRACE -Value ('browser:'+$FilePath)
}
''' + "\n& "+quote(ROOT / "deploy/start-docker.ps1")+" -ProjectRoot "+quote(tmp_path)+(" -ValidateOnly" if validate else "")+"\nexit $LASTEXITCODE\n",encoding="utf-8-sig")
    result=subprocess.run([SHELL,"-NoProfile","-ExecutionPolicy","Bypass","-File",str(script)],
        env={**os.environ,"MOCK_TRACE":str(trace),"MOCK_ENGINE":engine,"MOCK_BUILD_FAIL":"1" if build_fails else "0",
             "MOCK_BASE_MISSING":"1" if base_missing else "0","MOCK_PULL_FAIL":"1" if pull_fails else "0"},
        capture_output=True,timeout=30)
    return result,trace.read_text(encoding="utf-8-sig") if trace.exists() else ""


def test_validation_never_starts_engine_containers_or_browser(tmp_path):
    result,trace=run_launcher(tmp_path,validate=True)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'config --quiet' in trace
    assert 'info ' not in trace and ' up ' not in trace and 'browser:' not in trace


def test_full_mock_uses_private_overrides_and_opens_only_local_management(tmp_path):
    result,trace=run_launcher(tmp_path)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'deploy/local/docker.env' in trace and 'deploy/local/compose.local.yml' in trace
    assert ' up -d --build' in trace
    assert 'browser:http://127.0.0.1:8766/#token=fake_management_token_' in trace
    assert 'browser:http://127.0.0.1:6099' in trace
    assert '--with-worker' not in trace
    assert '\npull ' not in trace


def test_desktop_is_started_when_engine_initially_unavailable(tmp_path):
    result,trace=run_launcher(tmp_path,engine="warming")
    assert result.returncode==0,result.stdout+result.stderr
    assert 'Docker Desktop.exe' in trace and ' up -d --build' in trace


def test_missing_base_is_fetched_by_engine_before_build(tmp_path):
    result,trace=run_launcher(tmp_path,base_missing=True)
    assert result.returncode==0,result.stdout+result.stderr
    assert trace.index('pull python:3.12-slim') < trace.index(' up -d --build')


def test_failed_base_pull_never_starts_containers_or_browser(tmp_path):
    result,trace=run_launcher(tmp_path,base_missing=True,pull_fails=True)
    assert result.returncode!=0
    assert ' up ' not in trace and 'browser:' not in trace


@pytest.mark.parametrize("engine,build_fails",[("windows",False),("linux",True)])
def test_failure_does_not_open_pages_or_read_admin_token(tmp_path,engine,build_fails):
    result,trace=run_launcher(tmp_path,engine=engine,build_fails=build_fails)
    assert result.returncode!=0
    assert 'browser:' not in trace and ' exec ' not in trace
