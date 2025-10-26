# napcron

**napcron** is a small, Python-based, parallel replacement for `anacron`.
It ensures periodic jobs (daily, weekly, monthly, etc.) still run even if the system was powered off, suspended, or offline when they were scheduled.

Unlike `cron`, it records when tasks last succeeded and executes only those that are due.
Unlike `anacron`, it is easy to configure

---

## Installation

### From PyPI

```bash
python -m pip install --upgrade napcron
```

### From source (local checkout)

```bash
git clone https://github.com/alcalde/napcron.git
cd napcron
python -m pip install .
```

The `napcron` CLI becomes available on your `PATH` after installation.

---

## Usage

`napcron` is designed to be executed once per hour via `cron`, `systemd`, or any scheduler.
It reads a YAML configuration file, checks which tasks are due, evaluates their requirements, and executes them in parallel.

### Cron job example

Edit your user crontab (`crontab -e`):

```cron
@hourly napcron /home/user/.napcron.yaml
```

---

## Configuration

Example `/home/user/.napcron.yaml`:

```yaml
daily:
  - bash /home/user/a.sh:
      - internet
  - python $HOME/a.py: internet
  - ~/just_run_me.sh
  - ./also_okay:

weekly:
  - /cleanup_logs.sh: [internet, ac_power]

monthly:
  - python ~/rotate_backups.py
```

### Explanation

| Frequency | Description                                     | Interval |
| --------- | ----------------------------------------------- | -------- |
| `daily`   | Runs once every 24 hours since the last success | 1 day    |
| `weekly`  | Runs once every 7 days since the last success   | 7 days   |
| `monthly` | Runs once every 30 days                         | 30 days  |

---

## Features

* Runs missed periodic jobs (daily, weekly, monthly)
* Executes due tasks in parallel
* Supports configurable requirements:

  * `internet` – verify network connectivity
  * `ac_power` – ensure external power is connected
  * `battery` – placeholder for custom checks
* Simple YAML configuration
* Persistent state tracking in `~/.local/state/napcron/`
* Atomic file lock to prevent overlapping runs
* Safe dry-run mode (`--dry-run`)
* Only dependency: [PyYAML](https://pyyaml.org)

**Accepted YAML formats for each job:**

```yaml
- "cmd"                # No requirements
- cmd:                 # No requirements (no [] needed)
- cmd: internet        # Single requirement
- cmd: [internet, ac_power]  # Multiple requirements
```

---

## Requirements

Each task may specify one or more requirements that must be met before execution.
napcron provides the following built-in checks:

| Requirement | Description                                                                              | Platforms               |
| ----------- | ---------------------------------------------------------------------------------------- | ----------------------- |
| `internet`  | Checks for network connectivity by opening a TCP connection to 1.1.1.1:443 or 8.8.8.8:53 | All                     |
| `ac_power`  | Returns true if the system is running on external power (Linux, macOS, Windows)          | Linux / macOS / Windows |
| `battery`   | Placeholder requirement, always returns True                                             | All                     |

Custom requirement checks can easily be added by extending the `REQUIREMENTS` dictionary in `napcron.py`.

Example:

```python
def req_gpu_available(cmd: str) -> bool:
    return shutil.which("nvidia-smi") is not None

REQUIREMENTS["gpu"] = req_gpu_available
```

Used in YAML:

```yaml
daily:
  - python train.py: gpu
```

---

## State Management

napcron stores metadata about executed tasks in JSON format at:

```
~/.local/state/napcron/<config-name>.state.json
```

Each entry includes information such as last success, last attempt, and last exit code.

Example:

```json
{
  "tasks": {
    "daily::bash /home/daniel/a.sh": {
      "frequency": "daily",
      "cmd": "bash /home/daniel/a.sh",
      "last_success": "2025-10-26T10:00:00+00:00",
      "last_status": 0
    }
  }
}
```

---

## Command-line Options

| Option            | Description                                                                  |
| ----------------- | ---------------------------------------------------------------------------- |
| `--dry-run`       | Show which jobs would run, without executing or modifying state              |
| `--verbose`, `-v` | Print detailed logs                                                          |
| `--state PATH`    | Use a custom path for the JSON state file                                    |
| `--max-workers N` | Limit the number of parallel jobs (default: number of due tasks, maximum 32) |

---
