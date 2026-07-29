#!/opt/homebrew/bin/python3.12
"""Black-box smoke test for a running read-only PiNAS cleanup service."""

from __future__ import annotations

import argparse
import json
import stat
import sys
from pathlib import Path
from typing import Any
import urllib.error
import urllib.request
from urllib.parse import urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from snapshot_integrity import validate_snapshot_pair  # noqa: E402


class SmokeFailure(RuntimeError):
    """Raised when the running service violates a read-only invariant."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def request_json(
    base_url: str,
    path: str,
    *,
    origin: str | None = None,
    token: str | None = None,
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any], Any]:
    headers = {"Accept": "application/json"}
    if origin:
        headers["Origin"] = origin
    if token:
        headers["X-PiNAS-Session"] = token
    body = None
    method = "GET"
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode("utf-8")
        method = "POST"
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.load(response), response.headers
    except urllib.error.HTTPError as exc:
        try:
            response_payload = json.load(exc)
        except (json.JSONDecodeError, UnicodeDecodeError) as parse_error:
            raise SmokeFailure(
                f"{path} returned non-JSON HTTP {exc.code}"
            ) from parse_error
        return exc.code, response_payload, exc.headers
    except (OSError, urllib.error.URLError) as exc:
        raise SmokeFailure(f"cannot reach {base_url}{path}: {exc}") from exc


def error_code(payload: dict[str, Any]) -> str:
    error = payload.get("error")
    return str(error.get("code", "")) if isinstance(error, dict) else ""


def require_private_file(path: Path) -> None:
    require(path.exists(), f"missing private file: {path.name}")
    require(not path.is_symlink(), f"private file is a symlink: {path.name}")
    mode = stat.S_IMODE(path.stat().st_mode)
    require(mode == 0o600, f"private file mode is {mode:o}, expected 600")


def private_json(path: Path) -> dict[str, Any]:
    require_private_file(path)
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    require(isinstance(payload, dict), f"{path.name} is not a JSON object")
    return payload


def run(
    base_url: str,
    origin: str,
    *,
    expect_execution_enabled: bool = False,
) -> dict[str, int]:
    parsed = urlparse(base_url)
    require(
        parsed.scheme == "http"
        and parsed.hostname in {"127.0.0.1", "::1", "localhost"},
        "smoke test only accepts a loopback HTTP service",
    )
    base_url = base_url.rstrip("/")

    status, health, headers = request_json(base_url, "/health")
    require(status == 200 and health.get("ok") is True, "health check failed")
    require(
        health.get("executionEnabled") is expect_execution_enabled,
        "execution mode does not match smoke-test expectation",
    )
    require(
        health.get("inventoryCurrent") is True,
        "running inventory is not current",
    )
    require(headers.get("Cache-Control") == "no-store", "health is cacheable")

    status, denied, _ = request_json(base_url, "/v1/session")
    require(
        status == 403 and error_code(denied) == "origin_denied",
        "session endpoint accepted a missing Origin",
    )
    status, denied, _ = request_json(
        base_url,
        "/v1/session",
        origin="https://untrusted.invalid",
    )
    require(
        status == 403 and error_code(denied) == "origin_denied",
        "session endpoint accepted an untrusted Origin",
    )

    status, session, headers = request_json(
        base_url,
        "/v1/session",
        origin=origin,
    )
    token = session.get("sessionToken")
    require(status == 200 and session.get("ok") is True, "session failed")
    require(isinstance(token, str) and len(token) >= 32, "session token invalid")
    require(
        headers.get("Access-Control-Allow-Origin") == origin,
        "session CORS origin mismatch",
    )
    require(
        session.get("executionEnabled") is expect_execution_enabled,
        "session execution mode does not match smoke-test expectation",
    )
    require(session.get("inventoryCurrent") is True, "session inventory is stale")

    token_path = PROJECT_ROOT / ".runtime/control-token"
    require_private_file(token_path)

    status, snapshot_response, headers = request_json(
        base_url,
        "/v1/snapshot",
        origin=origin,
    )
    public = snapshot_response.get("snapshot")
    require(
        status == 200
        and snapshot_response.get("ok") is True
        and isinstance(public, dict),
        "snapshot endpoint failed",
    )
    require(
        headers.get("Access-Control-Allow-Origin") == origin,
        "snapshot CORS origin mismatch",
    )
    private = private_json(PROJECT_ROOT / ".runtime/resource-inventory.json")
    validate_snapshot_pair(public, private)

    token_on_disk = token_path.read_text(encoding="utf-8").strip()
    require(token_on_disk == token, "session token file does not match service")

    authenticated = {"origin": origin, "token": token}
    status, recovery, _ = request_json(
        base_url,
        "/v1/recovery",
        **authenticated,
    )
    recoveries = recovery.get("recoveries")
    require(
        status == 200 and isinstance(recoveries, list),
        "recovery inspection failed",
    )
    require(not recoveries, "unresolved cleanup transaction is present")

    status, gaps_response, _ = request_json(
        base_url,
        "/v1/protection-gaps",
        **authenticated,
    )
    gaps = gaps_response.get("gaps")
    require(
        status == 200 and isinstance(gaps, list),
        "H&R protection inspection failed",
    )
    missing = public.get("stats", {}).get("hrMissingQbTasks")
    require(
        isinstance(missing, int) and len(gaps) == missing,
        "H&R gap count does not match the public snapshot",
    )

    status, invalid_plan, _ = request_json(
        base_url,
        "/v1/plan",
        payload={"mode": "pause", "resourceIds": []},
        **authenticated,
    )
    require(
        status == 400 and error_code(invalid_plan) == "invalid_request",
        "invalid empty plan was not rejected",
    )

    if not expect_execution_enabled:
        for path, payload in (
            (
                "/v1/execute",
                {"planId": "smoke-missing", "confirmation": "smoke"},
            ),
            (
                "/v1/recovery",
                {
                    "transactionId": "smoke-missing",
                    "action": "rollback",
                    "confirmation": "smoke",
                },
            ),
        ):
            status, disabled, _ = request_json(
                base_url,
                path,
                payload=payload,
                **authenticated,
            )
            require(
                status == 501 and error_code(disabled) == "execution_disabled",
                f"{path} did not fail closed in read-only mode",
            )

    return {
        "resources": len(public["resources"]),
        "qbTasks": int(public["stats"]["qbTasks"]),
        "hrGaps": len(gaps),
        "unresolvedTransactions": len(recoveries),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--origin", default="http://localhost:3000")
    parser.add_argument(
        "--expect-execution-enabled",
        action="store_true",
        help="Validate a production control service without issuing a mutation.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        summary = run(
            args.base_url,
            args.origin,
            expect_execution_enabled=args.expect_execution_enabled,
        )
    except (SmokeFailure, ValueError, json.JSONDecodeError) as exc:
        print(f"READ_ONLY_SMOKE_FAILED: {exc}", file=sys.stderr)
        return 1
    print(
        "READ_ONLY_SMOKE_OK "
        f"resources={summary['resources']} "
        f"qbTasks={summary['qbTasks']} "
        f"hrGaps={summary['hrGaps']} "
        f"unresolvedTransactions={summary['unresolvedTransactions']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
