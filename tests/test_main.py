import json
import sys
import textwrap

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
