"""Regression checks for Windows double-click startup and broken venv homes."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("powershell.exe")
pytestmark = pytest.mark.skipif(os.name != "nt" or not POWERSHELL, reason="Windows launcher")


def test_existing_launcher_with_missing_base_python_is_not_healthy(tmp_path):
    scripts = tmp_path / "broken" / "Scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(sys.executable, scripts / "python.exe")
    (scripts.parent / "pyvenv.cfg").write_text(
        "home = C:\\missing-subtitle-python-311\ninclude-system-site-packages = false\nversion = 3.11.15\n",
        encoding="utf-8",
    )
    command = ". (Join-Path $env:SUBTITLE_TEST_ROOT 'launcher.ps1'); " \
              "if (Test-SubtitlePython (Join-Path $env:SUBTITLE_TEST_BROKEN 'python.exe')) {exit 9}; exit 0"
    environment = dict(os.environ, SUBTITLE_TEST_ROOT=str(ROOT), SUBTITLE_TEST_BROKEN=str(scripts))
    result = subprocess.run([POWERSHELL, "-NoProfile", "-Command", command], env=environment,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def test_double_click_entry_works_offline_from_another_working_directory(tmp_path):
    runtime = ROOT / ".runtime" / "venv" / "Scripts" / "python.exe"
    if not runtime.exists():
        pytest.skip("Run setup.bat once before testing the installed runtime")
    environment = dict(os.environ, UV_OFFLINE="1", REALTIME_SUBTITLE_NO_PAUSE="1",
                       PYTHONUTF8="1", LOCALAPPDATA=str(tmp_path))
    result = subprocess.run([os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", str(ROOT / "run.bat"),
                             "--smoke-gui"], cwd=tmp_path, env=environment,
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=25)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Setting up Python 3.11 and dependencies" not in result.stdout
    assert "No Python at" not in result.stderr
