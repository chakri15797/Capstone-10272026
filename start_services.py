import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List


ROOT = Path(__file__).resolve().parent
RUN_DIR = ROOT / ".run"
LOG_DIR = RUN_DIR / "logs"
STATE_FILE = RUN_DIR / "processes.json"

SERVICES = [
    {
        "name": "readmission-api",
        "command": ["kubectl", "port-forward", "-n", "default", "svc/readmission-risk-api", "8001:8000"],
        "match": ["port-forward", "svc/readmission-risk-api", "8001:8000"],
        "port": 8001,
    },
    {
        "name": "grafana",
        "command": ["kubectl", "port-forward", "-n", "monitoring", "svc/grafana", "3001:3000"],
        "match": ["port-forward", "svc/grafana", "3001:3000"],
        "port": 3001,
    },
    {
        "name": "prometheus",
        "command": ["kubectl", "port-forward", "-n", "monitoring", "svc/prometheus", "9091:9090"],
        "match": ["port-forward", "svc/prometheus", "9091:9090"],
        "port": 9091,
    },
    {
        "name": "pushgateway",
        "command": ["kubectl", "port-forward", "-n", "monitoring", "svc/pushgateway", "9092:9091"],
        "match": ["port-forward", "svc/pushgateway", "9092:9091"],
        "port": 9092,
    },
]


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


def _port_is_open(port: int) -> bool:
    with socket.socket() as connection:
        connection.settimeout(0.25)
        return connection.connect_ex(("127.0.0.1", port)) == 0


def _load_state() -> Dict[str, Any]:
    if not STATE_FILE.exists():
        return {"processes": []}
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Cannot read process state at {STATE_FILE}: {error}") from error
    if not isinstance(state, dict) or not isinstance(state.get("processes"), list):
        raise RuntimeError(f"Invalid process state at {STATE_FILE}.")
    return state


def _save_state(state: Dict[str, Any]) -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _validate_kubernetes() -> None:
    for namespace, service in (
        ("default", "readmission-risk-api"),
        ("monitoring", "grafana"),
        ("monitoring", "prometheus"),
        ("monitoring", "pushgateway"),
    ):
        subprocess.run(
            ["kubectl", "get", "service", service, "-n", namespace],
            cwd=ROOT,
            check=True,
        )


def _launch(
    name: str,
    command: List[str],
    match: List[str],
    state: Dict[str, Any],
) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{name}.log"
    log_file = log_path.open("a", encoding="utf-8")
    options: Dict[str, Any] = {
        "cwd": ROOT,
        "stdin": subprocess.DEVNULL,
        "stdout": log_file,
        "stderr": subprocess.STDOUT,
    }
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True

    try:
        process = subprocess.Popen(command, **options)
    finally:
        log_file.close()

    state["processes"].append(
        {
            "name": name,
            "pid": process.pid,
            "match": match,
            "log": str(log_path.relative_to(ROOT)),
        }
    )
    _save_state(state)
    print(f"Started {name} (PID {process.pid}); log: {log_path}")


def main() -> int:
    try:
        _validate_kubernetes()
        processes = _processes()
        state = _load_state()
        active_by_pid = {process["pid"]: process for process in processes}

        active_owned = []
        for record in state["processes"]:
            process = active_by_pid.get(record.get("pid"))
            if process and _matches(process, record.get("match", [])):
                active_owned.append(record)
        state["processes"] = active_owned

        for service in SERVICES:
            already_running = any(
                _matches(process, service["match"]) for process in processes
            )
            if not already_running and _port_is_open(service["port"]):
                raise RuntimeError(
                    f"Local port {service['port']} is already in use by another process."
                )

        model_match = ["readmission_model.py"]
        if any(_matches(process, model_match) for process in processes):
            print("Readmission training is already running; leaving it untouched.")
        elif any(record["name"] == "training" for record in active_owned):
            print("Readmission training was already started by this script.")
        else:
            _launch(
                "training",
                [sys.executable, str(ROOT / "readmission_model.py")],
                model_match,
                state,
            )

        for service in SERVICES:
            if any(_matches(process, service["match"]) for process in processes):
                print(f"{service['name']} port-forward is already running; leaving it untouched.")
                continue
            _launch(service["name"], service["command"], service["match"], state)

            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and not _port_is_open(service["port"]):
                current = next(
                    (item for item in state["processes"] if item["name"] == service["name"]),
                    None,
                )
                if current and current["pid"] not in {item["pid"] for item in _processes()}:
                    raise RuntimeError(
                        f"{service['name']} port-forward exited; see "
                        f"{ROOT / current['log']} for details."
                    )
                time.sleep(0.25)
            if not _port_is_open(service["port"]):
                raise RuntimeError(
                    f"{service['name']} did not open port {service['port']} within 15 seconds."
                )

        capture_match = ["capture_startup.py"]
        if any(_matches(process, capture_match) for process in processes):
            print("Startup capture is already running; leaving it untouched.")
        elif any(record["name"] == "startup-capture" for record in active_owned):
            print("Startup capture was already started by this script.")
        else:
            _launch(
                "startup-capture",
                [sys.executable, "-u", str(ROOT / "capture_startup.py")],
                capture_match,
                state,
            )

        if state["processes"]:
            _save_state(state)
            print(f"Managed processes are recorded in {STATE_FILE}.")
        else:
            print("All requested services were already running; nothing was started.")
        print("API:       http://127.0.0.1:8001")
        print("Grafana:   http://127.0.0.1:3001")
        print("Prometheus: http://127.0.0.1:9091")
        return 0
    except (OSError, subprocess.SubprocessError, RuntimeError, json.JSONDecodeError) as error:
        print(f"Could not start the project: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
