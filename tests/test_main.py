import json
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import napcron


def write_config(tmp_path, contents: str):
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(contents))
    return path


def run_main(monkeypatch, args):
    monkeypatch.setattr(sys, "argv", ["napcron.py", *args])
    with pytest.raises(SystemExit) as exc:
        napcron.main()
    return exc.value.code


def test_run_command_suppresses_output(monkeypatch):
    called = {}

    class DummyResult:
        returncode = 3

    def fake_run(cmd, shell, stdout, stderr):
        called["stdout"] = stdout
        called["stderr"] = stderr
        return DummyResult()

    monkeypatch.setattr(subprocess, "run", fake_run)
    rc = napcron.run_command("echo hi", verbose=False, dry_run=False)
    assert rc == 3
    assert called["stdout"] is subprocess.DEVNULL
    assert called["stderr"] is subprocess.DEVNULL


def test_run_command_passthrough_when_verbose(monkeypatch):
    called = {}

    class DummyResult:
        returncode = 0

    def fake_run(cmd, shell, stdout, stderr):
        called["stdout"] = stdout
        called["stderr"] = stderr
        return DummyResult()

    monkeypatch.setattr(subprocess, "run", fake_run)
    rc = napcron.run_command("echo hi", verbose=True, dry_run=False)
    assert rc == 0
    assert called["stdout"] is None
    assert called["stderr"] is None


def test_main_uses_default_config_when_missing(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("run_command should not be invoked when default config is empty")

    monkeypatch.setattr(napcron, "run_command", fail_if_called)

    default_cfg = home / ".napcron.yaml"
    assert not default_cfg.exists()

    exit_code = run_main(monkeypatch, [])
    assert exit_code == 0
    assert default_cfg.exists()
    assert default_cfg.read_text() == "daily:\n"

    state_path = home / ".local" / "state" / "napcron" / ".napcron.state.json"
    assert state_path.exists()


def test_main_runs_due_task_updates_state(tmp_path, monkeypatch):
    config = write_config(
        tmp_path,
        """
        daily:
          - echo task1
        """,
    )
    state = tmp_path / "state.json"
    calls = []

    def fake_run(cmd, verbose, dry_run):
        calls.append((cmd, verbose, dry_run))
        return 0

    monkeypatch.setattr(napcron, "run_command", fake_run)
    exit_code = run_main(monkeypatch, [str(config), "--state", str(state), "--max-workers", "1"])
    assert exit_code == 0
    assert calls == [("echo task1", False, False)]

    saved = json.loads(state.read_text())
    task_id = "daily::echo task1"
    assert task_id in saved["tasks"]
    entry = saved["tasks"][task_id]
    assert entry["last_status"] == 0
    assert entry["last_success"]
    assert entry["last_note"].startswith("finished_at=")


def test_main_skips_task_when_not_due(tmp_path, monkeypatch):
    config = write_config(
        tmp_path,
        """
        daily:
          - echo later
        """,
    )
    state = tmp_path / "state.json"
    recent = napcron.iso(napcron.now_utc())
    state.write_text(
        json.dumps(
            {
                "tasks": {
                    "daily::echo later": {
                        "frequency": "daily",
                        "cmd": "echo later",
                        "last_success": recent,
                        "last_attempt": recent,
                        "last_status": 0,
                        "last_note": "finished_at=recent",
                    }
                }
            }
        )
    )

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("run_command should not be invoked for not-due tasks")

    monkeypatch.setattr(napcron, "run_command", fail_if_called)
    exit_code = run_main(monkeypatch, [str(config), "--state", str(state)])
    assert exit_code == 0

    saved = json.loads(state.read_text())
    entry = saved["tasks"]["daily::echo later"]
    assert entry["last_success"] == recent  # unchanged


def test_main_marks_unmet_requirements_and_skips(tmp_path, monkeypatch):
    config = write_config(
        tmp_path,
        """
        daily:
          - guarded cmd:
              - internet
        """,
    )
    state = tmp_path / "state.json"

    monkeypatch.setitem(napcron.REQUIREMENTS, "internet", lambda _: False)

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("run_command should not be invoked when requirements fail")

    monkeypatch.setattr(napcron, "run_command", fail_if_called)
    exit_code = run_main(monkeypatch, [str(config), "--state", str(state), "--verbose"])
    assert exit_code == 0

    saved = json.loads(state.read_text())
    task_id = "daily::guarded cmd"
    assert task_id in saved["tasks"]
    entry = saved["tasks"][task_id]
    assert entry["last_success"] is None
    assert "unmet requirements" in entry["last_note"]


def test_is_due_respects_hourly_interval(monkeypatch):
    reference = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(napcron, "now_utc", lambda: reference)

    within_hour = napcron.iso(reference - timedelta(minutes=30))
    assert not napcron.is_due(within_hour, "hourly")

    older = napcron.iso(reference - timedelta(hours=2))
    assert napcron.is_due(older, "hourly")


def test_failed_task_waits_until_next_interval_without_flag(tmp_path, monkeypatch):
    config = write_config(
        tmp_path,
        """
        daily:
          - echo only_once
        """,
    )
    state = tmp_path / "state.json"
    reference = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(napcron, "now_utc", lambda: reference)

    last_attempt = napcron.iso(reference - timedelta(hours=1))
    state.write_text(
        json.dumps(
            {
                "tasks": {
                    "daily::echo only_once": {
                        "frequency": "daily",
                        "cmd": "echo only_once",
                        "last_success": None,
                        "last_attempt": last_attempt,
                        "last_status": 1,
                        "last_note": "finished_at=earlier",
                    }
                }
            }
        )
    )

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("run_command should not run failed task without rerun flag")

    monkeypatch.setattr(napcron, "run_command", fail_if_called)
    exit_code = run_main(monkeypatch, [str(config), "--state", str(state)])
    assert exit_code == 0


def test_rerun_onfail_flag_allows_immediate_retry(tmp_path, monkeypatch):
    config = write_config(
        tmp_path,
        """
        daily:
          - echo keep_trying:
              - rerun_onfail
        """,
    )
    state = tmp_path / "state.json"
    reference = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(napcron, "now_utc", lambda: reference)
    last_attempt = napcron.iso(reference - timedelta(hours=1))
    state.write_text(
        json.dumps(
            {
                "tasks": {
                    "daily::echo keep_trying": {
                        "frequency": "daily",
                        "cmd": "echo keep_trying",
                        "last_success": None,
                        "last_attempt": last_attempt,
                        "last_status": 1,
                        "last_note": "finished_at=earlier",
                    }
                }
            }
        )
    )

    calls = []

    def fake_run(cmd, verbose, dry_run):
        calls.append(cmd)
        return 0

    monkeypatch.setattr(napcron, "run_command", fake_run)
    exit_code = run_main(
        monkeypatch,
        [str(config), "--state", str(state), "--max-workers", "1"],
    )
    assert exit_code == 0
    assert calls == ["echo keep_trying"]

    saved = json.loads(state.read_text())
    entry = saved["tasks"]["daily::echo keep_trying"]
    assert entry["last_status"] == 0
    assert entry["last_success"] is not None
