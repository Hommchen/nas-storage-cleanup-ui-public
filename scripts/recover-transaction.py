#!/opt/homebrew/bin/python3.12
"""Inspect or explicitly resolve one fail-closed PiNAS cleanup transaction."""

from __future__ import annotations

import argparse
import json

from execution_engine import (
    ExecutionError,
    LocalRecoveryRunner,
    SSHRecoveryRunner,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan_id")
    parser.add_argument(
        "--action",
        choices=("inspect", "rollback", "finalize"),
        default="inspect",
    )
    parser.add_argument("--confirm-phrase", default="")
    parser.add_argument(
        "--ssh-host",
        default="nas-user@192.0.2.1",
    )
    parser.add_argument(
        "--local-nas",
        action="store_true",
        help="Run transaction inspection or recovery directly on the Pi.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        runner = (
            LocalRecoveryRunner()
            if args.local_nas
            else SSHRecoveryRunner(host=args.ssh_host)
        )
        result = runner(
            plan_id=args.plan_id,
            action=args.action,
            confirm_phrase=args.confirm_phrase,
        )
    except ExecutionError as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": exc.code,
                        "message": exc.message,
                    },
                },
                ensure_ascii=False,
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
