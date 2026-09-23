"""Repeat-safe start/stop/status for the local memory dashboard."""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from .runtime import PROJECT_ROOT


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("action", choices=["start", "stop", "status"], nargs="?", default="start")
    args = cli.parse_args()
    directory = PROJECT_ROOT / ".data/dashboard"
    directory.mkdir(parents=True, exist_ok=True)
    pidfile, logfile = directory / "server.pid", directory / "server.log"
    url = "http://127.0.0.1:18580"

    def current_pid():
        if not pidfile.exists():
            return None
        try:
            pid = int(pidfile.read_text())
            command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            if b"local_runtime.dashboard" in command:
                return pid
        except (OSError, ValueError):
            pass
        return None

    pid = current_pid()
    if args.action == "stop":
        if pid is None:
            print("Dashboard is not running")
            return 0
        os.kill(pid, signal.SIGTERM)
        for _ in range(120):
            if current_pid() is None:
                pidfile.unlink(missing_ok=True)
                print("Dashboard stopped; Qdrant is available to CLI commands")
                return 0
            time.sleep(0.5)
        print("Shutdown requested; waiting for current parsing request. Check " + str(logfile))
        return 1
    if pid is None and args.action == "start":
        environment = dict(os.environ)
        environment.setdefault("HF_HUB_OFFLINE", "1")
        with logfile.open("ab") as log:
            process = subprocess.Popen(
                [str(PROJECT_ROOT / ".venv/bin/python"), "-u", "-m", "local_runtime.dashboard"],
                cwd=PROJECT_ROOT,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
                env=environment,
            )
        pid = process.pid
        pidfile.write_text(str(pid))
    if pid is None:
        print("Dashboard is not running")
        return 1
    deadline = time.monotonic() + (90 if args.action == "start" else 3)
    while time.monotonic() < deadline:
        if current_pid() is None:
            print("Dashboard exited; inspect " + str(logfile))
            return 1
        try:
            with urllib.request.urlopen(url + "/api/overview", timeout=2) as response:
                info = json.load(response)
            print(f"Dashboard ready: {url} | memories={info['total']} | pid={pid}")
            return 0
        except (OSError, ValueError):
            time.sleep(0.5)
    print("Dashboard is starting or busy; inspect " + str(logfile))
    return 1


if __name__ == "__main__":
    sys.exit(main())
