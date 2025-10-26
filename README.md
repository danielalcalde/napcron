# napcron

**napcron** is a small, Python-based, parallel replacement for `anacron`.
It ensures periodic jobs (hourly, daily, weekly, monthly, etc.) still run even if the system was powered off, suspended, or offline when they were scheduled.

Unlike `cron`, it records when tasks last succeeded and executes only those that are due.
Unlike `anacron`, it is easy to configure.

---

## Installation

```bash
pip install napcron
```

---

## Usage

`napcron` is designed to be executed once per hour via `cron`, `systemd`, or any scheduler.
It reads a YAML configuration file, checks which tasks are due, evaluates their requirements, and executes them in parallel.

### Cron job example

Edit your user crontab (`crontab -e`):

```cron
@hourly napcron  # uses ~/.napcron.yaml by default
```

---

## Configuration

The default config file lives at `~/.napcron.yaml`; if it doesn't exist yet, running `napcron` once will create it.
```

From there you can extend it. Example `/home/user/.napcron.yaml`:

```yaml
hourly:
  - /usr/local/bin/refresh-cache.sh

daily:
  - bash /home/user/a.sh:
      - internet
  - python $HOME/a.py: internet
  - ~/just_run_me.sh

weekly:
  - /cleanup_logs.sh: [internet, ac_power, rerun_onfail]

monthly:
  - python ~/rotate_backups.py
```

### Explanation

| Frequency | Description                                                                      | Interval |
| --------- | -------------------------------------------------------------------------------- | -------- |
| `hourly`  | Runs once every hour; failed runs wait an hour unless `rerun_onfail` is present  | 1 hour   |
| `daily`   | Runs once every 24 hours; failed runs wait a day unless `rerun_onfail` is present | 1 day    |
| `weekly`  | Runs once every 7 days; failed runs wait a week unless `rerun_onfail` is present | 7 days   |
| `monthly` | Runs once every 30 days                                                          | 30 days  |

---

## Features

* Runs missed periodic jobs (hourly, daily, weekly, monthly)
* Executes due tasks in parallel
* Supports configurable requirements:

  * `internet` – verify network connectivity
  * `ac_power` – ensure external power is connected
  * `battery` – only runs when on battery
  * `rerun_onfail` – retry failed jobs immediately instead of waiting for the next interval
* Simple YAML configuration
* Persistent state tracking in `~/.local/state/napcron/`
* Atomic file lock to prevent overlapping runs
* Safe dry-run mode (`--dry-run`)

---

## Requirements

Each task may specify one or more requirements that must be met before execution.
napcron provides the following built-in checks:

| Requirement | Description                                                                              | Platforms               |
| ----------- | ---------------------------------------------------------------------------------------- | ----------------------- |
| `internet`     | Checks for network connectivity by opening a TCP connection to 1.1.1.1:443 or 8.8.8.8:53 | All                     |
| `ac_power`     | Returns true if the system is running on external power                                  | Linux / macOS / Windows |
| `battery`      | not ac_power                                                                             | Linux / macOS / Windows |
| `rerun_onfail` | Immediately retries a task on the next napcron invocation after a non-zero exit          | All                     |

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
  - python flaky.py:
      - rerun_onfail
```

## Command-line Options

| Option            | Description                                                                  |
| ----------------- | ---------------------------------------------------------------------------- |
| `--dry-run`       | Show which jobs would run, without executing or modifying state              |
| `--verbose`, `-v` | Print detailed logs                                                          |
| `--state PATH`    | Use a custom path for the JSON state file                                    |
| `--max-workers N` | Limit the number of parallel jobs (default: number of due tasks, maximum 32) |

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
