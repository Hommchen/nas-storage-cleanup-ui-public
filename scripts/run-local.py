#!/opt/homebrew/bin/python3.12
"""Run the PiNAS cleanup UI and loopback control service together."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYTHON = "/opt/homebrew/bin/python3.12"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--enable-execution",
        action="store_true",
        help="Allow confirmation-gated NAS mutations.",
    )
    parser.add_argument(
        "--production",
        action="store_true",
        help="Build and run the production frontend.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Host-side JSON configuration file.",
    )
    return parser.parse_args()


def terminate(processes: list[subprocess.Popen[bytes]]) -> None:
    for process in processes:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
    deadline = time.monotonic() + 5
    for process in processes:
        if process.poll() is not None:
            continue
        try:
            process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)


def main() -> int:
    args = parse_args()
    npm = shutil.which("npm")
    if not npm:
        raise SystemExit("npm is required")
    if args.production:
        subprocess.run(
            [npm, "run", "build"],
            cwd=PROJECT_ROOT,
            check=True,
        )

    control_command = [
        PYTHON,
        str(PROJECT_ROOT / "scripts/control_server.py"),
    ]
    if args.enable_execution:
        control_command.append("--enable-execution")
    if args.config:
        control_command.extend(["--config", str(args.config)])
    frontend_command = [
        npm,
        "run",
        "start" if args.production else "dev",
    ]
    environment = os.environ.copy()
    processes = [
        subprocess.Popen(
            control_command,
            cwd=PROJECT_ROOT,
            env=environment,
            start_new_session=True,
        ),
        subprocess.Popen(
            frontend_command,
            cwd=PROJECT_ROOT,
            env=environment,
            start_new_session=True,
        ),
    ]

    stopping = False

    def stop_handler(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)
    mode = "已启用最终确认" if args.enable_execution else "只读预演"
    print(f"NAS 清理台已启动：http://localhost:3000/（{mode}）")
    try:
        while not stopping:
            for process in processes:
                code = process.poll()
                if code is not None:
                    return code
            time.sleep(0.25)
    finally:
        terminate(processes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
