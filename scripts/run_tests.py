"""Run project tests with isolated plugins and workspace-owned temporary files."""
import os
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
temporary_root = (ROOT / ".pytest-tmp").resolve()
if not temporary_root.is_relative_to(ROOT):
    raise RuntimeError("测试临时目录必须位于项目目录内")
temporary_root.mkdir(parents=True, exist_ok=True)
base = (temporary_root / ("run-" + uuid4().hex[:12])).resolve()
if not base.is_relative_to(temporary_root):
    raise RuntimeError("无效的测试目录")
os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
sys.path.insert(0, str(ROOT / "src"))

import pytest

if __name__ == "__main__":
    os.chdir(ROOT)
    raise SystemExit(pytest.main(["--basetemp=" + str(base), "--tb=short", *sys.argv[1:]]))
