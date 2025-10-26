#!/usr/bin/env python3
"""
napcron — tiny, parallel, poor-man's anacron with requirements.

YAML examples (command key → requirement(s)):

daily:
  - bash a.sh:
      - internet
  - python a.py: internet       # single requirement as a string
  - ./just_run_me.sh            # short string form: no requirements
  - ./also_okay:                # mapping with None: no requirements
weekly:
  - ./cleanup_logs.sh: [internet, ac_power]
monthly:
  - rotate_backups.py:

Run hourly via cron. Only runs tasks that are *due* (daily/weekly/monthly) and whose
requirements pass.

Usage:
  napcron.py [config]
             [--state /path/to/state.json]
             [--dry-run]
             [--verbose|-v]
             [--max-workers N]

When no config path is provided, ~/.napcron.yaml is used and created automatically if missing.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import socket
import subprocess
import sys
import time
from pprint import pprint
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Tuple, Optional

# ------------------------- Frequencies -------------------------
FREQS: Dict[str, timedelta] = {
    "hourly": timedelta(hours=1),
    "daily": timedelta(days=1),
    "weekly": timedelta(days=7),
    "monthly": timedelta(days=30),  # anacron-like cadence
}

DEFAULT_CONFIG_BASENAME = ".napcron.yaml"
DEFAULT_CONFIG_TEMPLATE = "daily:\n"

# ------------------------- Config helpers ----------------------
def default_config_path() -> str:
    return os.path.abspath(os.path.join(os.path.expanduser("~"), DEFAULT_CONFIG_BASENAME))


def ensure_config_file(path: str, contents: str = DEFAULT_CONFIG_TEMPLATE) -> None:
    if os.path.exists(path):
        return
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(contents)
    except OSError as exc:
        print(f"Unable to create default config at {path}: {exc}", file=sys.stderr)
        sys.exit(1)

# ------------------------- Time helpers ------------------------
def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None

def parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None

def is_due(last_success_iso: Optional[str], freq: str) -> bool:
    """True if enough time elapsed since last_success for this frequency."""
    interval = FREQS[freq]
    if not last_success_iso:
        return True
    last = parse_iso(last_success_iso)
    if not last:
        return True
    return (now_utc() - last) >= interval

# ------------------------- Requirements ------------------------
def req_internet(_: str) -> bool:
    """True if at least one quick TCP connect succeeds (robust to DNS/firewall quirks)."""
    targets = [("1.1.1.1", 443), ("8.8.8.8", 53)]
    for host, port in targets:
        try:
            with socket.create_connection((host, port), timeout=2):
                return True
        except OSError:
            continue
    return False

def _linux_ac_online() -> Optional[bool]:
    base = "/sys/class/power_supply"
    if not os.path.isdir(base):
        return None
    try:
        # Prefer type=Mains + online
        for name in os.listdir(base):
            tfile = os.path.join(base, name, "type")
            ofile = os.path.join(base, name, "online")
            if os.path.isfile(tfile):
                with open(tfile, "r", encoding="utf-8", errors="ignore") as f:
                    typ = f.read().strip().lower()
                if typ == "mains" and os.path.isfile(ofile):
                    with open(ofile, "r", encoding="utf-8", errors="ignore") as f:
                        return f.read().strip() == "1"
        # Fallback: charging/full battery → likely on external power
        for name in os.listdir(base):
            sfile = os.path.join(base, name, "status")
            if os.path.isfile(sfile):
                with open(sfile, "r", encoding="utf-8", errors="ignore") as f:
                    status = f.read().strip().lower()
                if status in ("charging", "full"):
                    return True
        return None
    except Exception:
        return None

def _macos_ac_online() -> Optional[bool]:
    try:
        out = subprocess.check_output(["pmset", "-g", "batt"], text=True, timeout=3, stderr=subprocess.DEVNULL)
        first = out.splitlines()[0].lower()
        if "ac power" in first:
            return True
        if "battery power" in first:
            return False
        return None
    except Exception:
        return None

def _windows_ac_online() -> Optional[bool]:
    try:
        import ctypes
        class SYSTEM_POWER_STATUS(ctypes.Structure):
            _fields_ = [
                ("ACLineStatus", ctypes.c_byte),
                ("BatteryFlag", ctypes.c_byte),
                ("BatteryLifePercent", ctypes.c_byte),
                ("Reserved1", ctypes.c_byte),
                ("BatteryLifeTime", ctypes.c_ulong),
                ("BatteryFullLifeTime", ctypes.c_ulong),
            ]
        status = SYSTEM_POWER_STATUS()
        if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
            if status.ACLineStatus == 1:
                return True
            if status.ACLineStatus == 0:
                return False
        return None
    except Exception:
        return None

def _ac_power_status() -> Optional[bool]:
    """Best-effort detection of AC power. Returns True, False or None if unknown."""
    if sys.platform.startswith("linux"):
        return _linux_ac_online()
    if sys.platform == "darwin":
        return _macos_ac_online()
    if sys.platform.startswith("win"):
        return _windows_ac_online()
    return None

def req_ac_power(_: str) -> bool:
    """True when system is on external/AC power. If unknown, return False."""
    return _ac_power_status() is True

def req_battery(_: str) -> bool:
    """True when AC power is explicitly reported as absent."""
    status = _ac_power_status()
    if status is None:
        return False
    return status is False

REQUIREMENTS = {
    "internet": req_internet,
    "ac_power": req_ac_power,
    "battery": req_battery,
}

# Requirement-like flags that toggle behavior but should not be treated as predicates.
SPECIAL_REQUIREMENT_FLAGS = {
    "rerun_onfail",  # opt-in to legacy "retry failed tasks immediately" logic
}

# ------------------------- YAML parsing ------------------------
def load_yaml(path: str) -> Dict[str, List[Dict[str, List[str]]]]:
    """
    Normalize YAML into: { 'daily': [ {'cmd': str, 'requires': [str,...]} ], ... }

    Accepted job forms:
      - "cmd"               → no requirements
      - cmd:                → no requirements (no [] needed)
      - cmd: internet       → single string requirement
      - cmd: [a, b, c]      → multiple requirements
    """
    try:
        import yaml  # local import for clear error if missing
    except ImportError:
        print("ERROR: PyYAML is required. Install with: pip install pyyaml", file=sys.stderr)
        sys.exit(2)

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    normalized: Dict[str, List[Dict[str, List[str]]]] = {}
    for key, val in (data or {}).items():
        freq = str(key).lower()
        if freq not in FREQS or not isinstance(val, list):
            continue
        jobs: List[Dict[str, List[str]]] = []
        for item in val:
            if isinstance(item, dict):
                # single-key mapping: { "cmd": <None | str | list> }
                for cmd, reqs in item.items():
                    cmd_str = str(cmd)
                    if reqs is None:
                        req_list: List[str] = []
                    elif isinstance(reqs, str):
                        req_list = [reqs.lower()]
                    elif isinstance(reqs, list):
                        req_list = [str(r).lower() for r in reqs]
                    else:
                        continue  # bad shape
                    jobs.append({"cmd": cmd_str, "requires": req_list})
            elif isinstance(item, str):
                jobs.append({"cmd": item, "requires": []})  # short form
        normalized[freq] = jobs
    return normalized

# ------------------------- State I/O ---------------------------
def default_state_path(config_path: str) -> str:
    base = os.path.splitext(os.path.basename(config_path))[0]
    state_dir = os.path.join(os.path.expanduser("~"), ".local", "state", "napcron")
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, f"{base}.state.json")

def load_state(path: str) -> Dict:
    if not os.path.exists(path):
        return {"tasks": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"tasks": {}}

def save_state(path: str, state: Dict) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp, path)

# ------------------------- Locking (atomic) -------------------
def acquire_lock(state_path: str) -> Optional[str]:
    """Atomic lock via O_EXCL. Locks older than 2h are considered stale and replaced."""
    lock_path = state_path + ".lock"

    def _try_create() -> bool:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w") as f:
                f.write(str(os.getpid()))
            return True
        except FileExistsError:
            return False

    if _try_create():
        return lock_path

    try:
        age = time.time() - os.path.getmtime(lock_path)
        if age >= 2 * 3600:
            os.remove(lock_path)
            if _try_create():
                return lock_path
    except Exception:
        pass

    return None

def release_lock(lock_path: Optional[str]) -> None:
    if lock_path and os.path.exists(lock_path):
        try:
            os.remove(lock_path)
        except Exception:
            pass

# ------------------------- Execution --------------------------
def run_command(cmd: str, verbose: bool, dry_run: bool) -> int:
    ts = datetime.now().isoformat(timespec="seconds")
    if verbose or dry_run:
        print(f"[{ts}] RUN: {cmd}{' (dry-run)' if dry_run else ''}")
    if dry_run:
        return 0
    stdout = None if verbose else subprocess.DEVNULL
    stderr = None if verbose else subprocess.DEVNULL
    return subprocess.run(cmd, shell=True, stdout=stdout, stderr=stderr).returncode

# ------------------------- Main --------------------------------
def main() -> None:
    default_cfg = default_config_path()
    ap = argparse.ArgumentParser()
    ap.add_argument("config", nargs="?", help=f"Path to YAML config file (default: {default_cfg})")
    ap.add_argument("--state", help="Path to JSON state file (optional)")
    ap.add_argument("--dry-run", action="store_true", help="Print what would run; do NOT change state")
    ap.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    ap.add_argument("--max-workers", type=int, default=0, help="Max parallel jobs (default: #due tasks, cap 32)")
    args = ap.parse_args()

    cfg_arg = args.config or default_cfg
    cfg_path = os.path.abspath(os.path.expanduser(cfg_arg))
    if args.config is None:
        ensure_config_file(cfg_path)
    if not os.path.exists(cfg_path):
        print(f"Config not found: {cfg_path}", file=sys.stderr)
        sys.exit(1)

    state_arg = os.path.expanduser(args.state) if args.state else default_state_path(cfg_path)
    state_path = os.path.abspath(state_arg)
    state_parent = os.path.dirname(state_path)
    if state_parent and not os.path.isdir(state_parent):
        os.makedirs(state_parent, exist_ok=True)
    lock_path = acquire_lock(state_path)
    if lock_path is None:
        if args.verbose:
            print("Another instance appears to be running. Exiting.")
        sys.exit(0)

    try:
        config = load_yaml(cfg_path)
        state = load_state(state_path)

        if args.verbose:
            print(f"Loading state from {state_path}")
            pprint(state)

        tasks_state: Dict[str, Dict] = state.setdefault("tasks", {})

        # Build list of due tasks (dedup by task_id = "freq::cmd")
        due: List[Tuple[str, str]] = []  # (task_id, cmd)
        seen = set()

        for freq, jobs in config.items():
            for job in jobs:
                cmd = job["cmd"]
                reqs_all = job.get("requires", []) or []
                flags = {r for r in reqs_all if r in SPECIAL_REQUIREMENT_FLAGS}
                reqs = [r for r in reqs_all if r not in SPECIAL_REQUIREMENT_FLAGS]
                rerun_onfail = "rerun_onfail" in flags
                task_id = f"{freq}::{cmd}"

                entry = tasks_state.setdefault(task_id, {
                    "frequency": freq,
                    "cmd": cmd,
                    "last_success": None,
                    "last_attempt": None,
                    "last_status": None,
                    "last_note": None,
                })
                entry["frequency"] = freq
                entry["cmd"] = cmd

                last_success_iso = entry.get("last_success")
                last_attempt_iso = entry.get("last_attempt")
                last_status = entry.get("last_status")

                ref_time = last_success_iso
                if not rerun_onfail and last_status not in (None, 0):
                    ref_time = last_attempt_iso or last_success_iso

                if not is_due(ref_time, freq):
                    if args.verbose:
                        print(f"SKIP (not due): [{freq}] {cmd}")
                    continue

                # Check requirements (unknown names count as unmet). Each function takes one arg (cmd).
                unmet = []
                for r in (reqs or []):
                    fn = REQUIREMENTS.get(r)
                    ok = False
                    if fn:
                        try:
                            ok = bool(fn(cmd))
                        except Exception:
                            ok = False
                    if not fn or not ok:
                        unmet.append(r)
                if unmet:
                    if args.verbose:
                        print(f"SKIP (requirements not met: {unmet}): {cmd}")
                    entry["last_note"] = f"skipped: unmet requirements {unmet}"
                    continue

                if task_id not in seen:
                    seen.add(task_id)
                    due.append((task_id, cmd))

        if args.verbose:
            print(f"Due tasks: {len(due)}")

        if not due:
            if not args.dry_run:
                save_state(state_path, state)
            sys.exit(0)

        max_workers = args.max_workers if args.max_workers > 0 else min(32, len(due))

        # Workers only report; main thread mutates state
        def _job(task_id: str, cmd: str) -> Tuple[str, int, str, str]:
            started = iso(now_utc()) or ""
            rc = run_command(cmd, verbose=args.verbose, dry_run=args.dry_run)
            finished = iso(now_utc()) or ""
            return (task_id, rc, started, finished)

        results: List[Tuple[str, int, str, str]] = []
        with cf.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(_job, tid, cmd) for (tid, cmd) in due]
            for fut in cf.as_completed(futures):
                results.append(fut.result())

        # Apply results (no state writes on dry-run)
        exit_code = 0
        if not args.dry_run:
            for (task_id, rc, started, finished) in results:
                e = tasks_state[task_id]
                e["last_attempt"] = started
                e["last_status"] = int(rc)
                e["last_note"] = f"finished_at={finished}"
                if rc == 0:
                    e["last_success"] = finished
                else:
                    if exit_code == 0:
                        exit_code = rc
                if args.verbose:
                    print(f"DONE [{e['frequency']}]: {e['cmd']} -> {'OK' if rc == 0 else f'FAIL('+str(rc)+')'}")
        else:
            if args.verbose:
                for (task_id, _, started, _) in results:
                    e = tasks_state[task_id]
                    print(f"DRY-RUN (would run) [{e['frequency']}]: {e['cmd']} (planned_start={started})")

        if not args.dry_run:
            save_state(state_path, state)
        sys.exit(exit_code)

    finally:
        release_lock(lock_path)

if __name__ == "__main__":
    main()
