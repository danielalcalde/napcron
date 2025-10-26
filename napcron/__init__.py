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
  napcron.py /path/to/tasks.yaml
             [--state /path/to/state.json]
             [--dry-run]
             [--verbose|-v]
             [--max-workers N]
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
    "daily": timedelta(days=1),
    "weekly": timedelta(days=7),
    "monthly": timedelta(days=30),  # anacron-like cadence
}

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

def req_ac_power(_: str) -> bool:
    """True when system is on external/AC power. If unknown, return False."""
    if sys.platform.startswith("linux"):
        res = _linux_ac_online()
    elif sys.platform == "darwin":
        res = _macos_ac_online()
    elif sys.platform.startswith("win"):
        res = _windows_ac_online()
    else:
        res = None
    return bool(res) if res is not None else False

def req_battery(_: str) -> bool:
    """Stub; always True. Replace with a real threshold check if needed."""
    return True

REQUIREMENTS = {
    "internet": req_internet,
    "ac_power": req_ac_power,
    "battery": req_battery,
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
    return subprocess.run(cmd, shell=True).returncode

# ------------------------- Main --------------------------------
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("config", help="Path to YAML config file")
    ap.add_argument("--state", help="Path to JSON state file (optional)")
    ap.add_argument("--dry-run", action="store_true", help="Print what would run; do NOT change state")
    ap.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    ap.add_argument("--max-workers", type=int, default=0, help="Max parallel jobs (default: #due tasks, cap 32)")
    args = ap.parse_args()

    cfg_path = os.path.abspath(args.config)
    if not os.path.exists(cfg_path):
        print(f"Config not found: {cfg_path}", file=sys.stderr)
        sys.exit(1)

    state_path = os.path.abspath(args.state) if args.state else default_state_path(cfg_path)
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
                reqs = job.get("requires", [])
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

                if not is_due(entry["last_success"], freq):
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

        save_state(state_path, state)
        sys.exit(exit_code)

    finally:
        release_lock(lock_path)

if __name__ == "__main__":
    main()
