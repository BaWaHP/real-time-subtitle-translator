"""Regression checks for Windows double-click startup and broken venv homes."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXE_NAME = "Real-Time Subtitle Translator.exe"
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


@pytest.mark.parametrize("entry", ["run.bat", EXE_NAME])
def test_double_click_entry_works_offline_from_another_working_directory(tmp_path, entry):
    runtime = ROOT / ".runtime" / "venv" / "Scripts" / "python.exe"
    if not runtime.exists():
        pytest.skip("Run setup.bat once before testing the installed runtime")
    environment = dict(os.environ, UV_OFFLINE="1", REALTIME_SUBTITLE_NO_PAUSE="1",
                       PYTHONUTF8="1", LOCALAPPDATA=str(tmp_path))
    command = [str(ROOT / entry), "--smoke-gui"]
    if entry.endswith(".bat"):
        command = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", *command]
    screenshot = ROOT / "artifacts" / "main-window.png"
    previous = screenshot.stat().st_mtime_ns if screenshot.exists() else None
    result = subprocess.run(command, cwd=tmp_path, env=environment,
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=25)
    assert result.returncode == 0, result.stdout + result.stderr
    assert screenshot.stat().st_mtime_ns != previous
    assert not (tmp_path / "artifacts").exists()
    if entry.endswith(".bat"):
        assert "Setting up Python 3.11 and dependencies" not in result.stdout
        assert "No Python at" not in result.stderr


@pytest.fixture
def exe_project(tmp_path):
    project = tmp_path / "启动测试 项目"
    project.mkdir()
    for name in (EXE_NAME, "run.ps1"):
        shutil.copyfile(ROOT / name, project / name)
    (project / "launcher.ps1").write_text(
        "function Get-SubtitlePython { param([string]$ProjectRoot) $env:SUBTITLE_TEST_PYTHON }\n",
        encoding="ascii",
    )
    (project / "main.py").write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "Path(os.environ['SUBTITLE_TEST_OUTPUT']).write_text(\n"
        "    json.dumps({'argv': sys.argv, 'cwd': os.getcwd()}, ensure_ascii=False), encoding='utf-8')\n"
        "print(os.environ['SUBTITLE_TEST_DIAGNOSTIC'], file=sys.stderr)\n"
        "raise SystemExit(37)\n",
        encoding="ascii",
    )
    return project


def _run_exe(executable, cwd, args=(), **env_vars):
    environment = dict(os.environ, REALTIME_SUBTITLE_NO_PAUSE="1", **env_vars)
    return subprocess.run([str(executable), *args], cwd=cwd, env=environment, capture_output=True,
                          text=True, encoding="utf-8", timeout=15)


def test_exe_preserves_arguments_exit_code_and_failure_diagnostic(tmp_path, exe_project):
    output = tmp_path / "参数记录.json"
    arguments = ["中文参数", "two words", 'a"b', "", "ends with a backslash \\", '$(throw "must remain text")']
    diagnostic = "启动测试：保留中文错误"
    result = _run_exe(exe_project / EXE_NAME, tmp_path, arguments, SUBTITLE_TEST_PYTHON=sys.executable,
                      SUBTITLE_TEST_OUTPUT=str(output), SUBTITLE_TEST_DIAGNOSTIC=diagnostic)
    assert result.returncode == 37, result.stdout + result.stderr
    recorded = json.loads(output.read_text(encoding="utf-8"))
    assert recorded["argv"][1:] == arguments
    assert Path(recorded["argv"][0]).resolve() == exe_project / "main.py"
    assert Path(recorded["cwd"]).resolve() == exe_project.resolve()
    assert diagnostic in result.stderr


def test_exe_reports_missing_project_files_next_to_executable(tmp_path, exe_project):
    executable = tmp_path / EXE_NAME
    shutil.copyfile(exe_project / EXE_NAME, executable)
    result = _run_exe(executable, exe_project)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "同一目录" in result.stderr
