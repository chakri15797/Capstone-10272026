import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List


ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / ".run" / "processes.json"


def _processes() -> List[Dict[str, Any]]:
    if os.name == "nt":
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "Get-CimInstance Win32_Process | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        if not result.stdout.strip():
            return []
        records = json.loads(result.stdout)
        if isinstance(records, dict):
            records = [records]
        return [
            {"pid": int(record["ProcessId"]), "command": record.get("CommandLine") or ""}
            for record in records
            if record.get("ProcessId") is not None
        ]

    result = subprocess.run(
        ["ps", "-eo", "pid=,args="],
        check=True,
        capture_output=True,
        text=True,
    )
    processes = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) == 2:
            processes.append({"pid": int(parts[0]), "command": parts[1]})
    return processes


def _matches(process: Dict[str, Any], needles: List[str]) -> bool:
    command = process["command"].lower()
    return all(needle.lower() in command for needle in needles)


def _process_group_exists(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _stop(record: Dict[str, Any], processes: List[Dict[str, Any]]) -> None:
    pid = int(record["pid"])
    process = next((item for item in processes if item["pid"] == pid), None)
    if process is None or not _matches(process, record.get("match", [])):
        print(f"Skipped {record.get('name', 'process')} (PID {pid}); it is no longer the recorded process.")
        return

    if os.name == "nt":
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            remaining = next((item for item in _processes() if item["pid"] == pid), None)
            if remaining and _matches(remaining, record.get("match", [])):
                raise RuntimeError(
                    f"Could not stop {record.get('name')} (PID {pid}): "
                    f"{result.stderr.strip() or result.stdout.strip()}"
                )
    else:
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _process_group_exists(pid):
            time.sleep(0.1)
        if _process_group_exists(pid):
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    print(f"Stopped {record.get('name', 'process')} (PID {pid}).")


def main() -> int:
    if not STATE_FILE.exists():
        print("No processes launched by start_services.py are recorded; nothing to stop.")
        return 0

    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or not isinstance(state.get("processes"), list):
            raise RuntimeError(f"Invalid process state at {STATE_FILE}.")

        processes = _processes()
        for record in reversed(state["processes"]):
            _stop(record, processes)

        STATE_FILE.unlink()
        print("Stopped the processes launched by start_services.py. Existing Kubernetes deployments were left running.")
        return 0
    except (OSError, subprocess.SubprocessError, RuntimeError, json.JSONDecodeError, KeyError, TypeError) as error:
        print(f"Could not stop all managed processes: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
