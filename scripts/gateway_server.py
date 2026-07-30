#!/opt/homebrew/bin/python3.12
"""LAN-only same-origin gateway for the PiNAS cleanup UI."""

from __future__ import annotations

import argparse
import hmac
from http import HTTPStatus
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


DEFAULT_HOST = "192.0.2.1"
DEFAULT_PORT = 3000
MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
DEFAULT_UPSTREAM_TIMEOUT_SECONDS = 30
CONTROL_UPSTREAM_TIMEOUT_SECONDS = 900
DEFAULT_BRIDGE_NETWORK = "172.17.0.0/16"
DEFAULT_BRIDGE_TOKEN_FILE = (
    "/mnt/sdc/library-tools/storage-cleanup-ui/shared/runtime/control-token"
)
ALLOWED_CLIENTS = (
    ipaddress.ip_network("192.168.3.0/24"),
    ipaddress.ip_network("127.0.0.0/8"),
)
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def backend_for(path: str) -> tuple[str, int, str, bool]:
    parsed = urlsplit(path)
    is_control = parsed.path == "/control" or parsed.path.startswith(
        "/control/"
    )
    if is_control:
        stripped = parsed.path[len("/control") :] or "/"
        upstream_path = urlunsplit(
            ("", "", stripped, parsed.query, parsed.fragment)
        )
        return "127.0.0.1", 8765, upstream_path, True
    return "127.0.0.1", 3001, path, False


def client_allowed(address: str) -> bool:
    try:
        client = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(client in network for network in ALLOWED_CLIENTS)


def bridge_client_allowed(
    address: str,
    provided_token: str | None,
    *,
    bridge_network: str,
    bridge_token_file: str,
) -> bool:
    """Authorize only the MoviePilot Docker bridge with the live control token."""
    try:
        client = ipaddress.ip_address(address)
        network = ipaddress.ip_network(bridge_network)
    except ValueError:
        return False
    if client not in network or not provided_token:
        return False
    try:
        expected_token = Path(bridge_token_file).read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return bool(expected_token) and hmac.compare_digest(
        provided_token,
        expected_token,
    )


def request_allowed(
    address: str,
    *,
    is_control: bool,
    bridge_token: str | None,
    bridge_network: str,
    bridge_token_file: str,
) -> bool:
    if client_allowed(address):
        return True
    if not is_control:
        return False
    return bridge_client_allowed(
        address,
        bridge_token,
        bridge_network=bridge_network,
        bridge_token_file=bridge_token_file,
    )


def upstream_timeout(is_control: bool) -> int:
    """Give mutating control requests enough time to finish their read-back."""
    return (
        CONTROL_UPSTREAM_TIMEOUT_SECONDS
        if is_control
        else DEFAULT_UPSTREAM_TIMEOUT_SECONDS
    )


def handler_class(
    connection_factory: type[HTTPConnection] = HTTPConnection,
    *,
    bridge_network: str = DEFAULT_BRIDGE_NETWORK,
    bridge_token_file: str = DEFAULT_BRIDGE_TOKEN_FILE,
):
    class GatewayHandler(BaseHTTPRequestHandler):
        server_version = "PiNASCleanupGateway/1"
        sys_version = ""

        def log_message(self, format_string: str, *args: Any) -> None:
            print(
                f"[gateway] {self.client_address[0]} "
                f"{format_string % args}"
            )

        def _send_error(
            self,
            status: int,
            message: str,
            *,
            json_response: bool = False,
        ) -> None:
            if json_response:
                body = json.dumps(
                    {
                        "ok": False,
                        "error": {
                            "code": "upstream_unavailable",
                            "message": message,
                        },
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
                content_type = "application/json; charset=utf-8"
            else:
                body = message.encode("utf-8")
                content_type = "text/plain; charset=utf-8"
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _proxy(self) -> None:
            host, port, path, is_control = backend_for(self.path)
            if not request_allowed(
                self.client_address[0],
                is_control=is_control,
                bridge_token=self.headers.get("X-PiNAS-Bridge-Token"),
                bridge_network=bridge_network,
                bridge_token_file=bridge_token_file,
            ):
                self._send_error(
                    HTTPStatus.FORBIDDEN,
                    "LAN access denied",
                    json_response=is_control,
                )
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._send_error(
                    HTTPStatus.BAD_REQUEST,
                    "Invalid request",
                    json_response=is_control,
                )
                return
            if length < 0 or length > MAX_REQUEST_BYTES:
                self._send_error(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    "Request too large",
                    json_response=is_control,
                )
                return
            body = self.rfile.read(length) if length else None
            headers = {
                key: value
                for key, value in self.headers.items()
                if key.lower() not in HOP_BY_HOP_HEADERS
                and key.lower()
                not in {
                    "host",
                    "content-length",
                    "origin",
                    "x-pinas-bridge-token",
                }
            }
            headers["Host"] = f"{host}:{port}"
            headers["X-Forwarded-For"] = self.client_address[0]
            if body is not None:
                headers["Content-Length"] = str(len(body))
            if is_control:
                headers["Origin"] = "http://localhost:3000"
            connection = connection_factory(
                host,
                port,
                timeout=upstream_timeout(is_control),
            )
            try:
                connection.request(
                    self.command,
                    path,
                    body=body,
                    headers=headers,
                )
                response = connection.getresponse()
                response_body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(response_body) > MAX_RESPONSE_BYTES:
                    self._send_error(
                        HTTPStatus.BAD_GATEWAY,
                        "Upstream response too large",
                        json_response=is_control,
                    )
                    return
                self.send_response(response.status)
                for key, value in response.getheaders():
                    lowered = key.lower()
                    if (
                        lowered in HOP_BY_HOP_HEADERS
                        or lowered == "content-length"
                        or lowered.startswith("access-control-")
                    ):
                        continue
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(response_body)))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(response_body)
            except OSError:
                self._send_error(
                    HTTPStatus.BAD_GATEWAY,
                    "PiNAS service unavailable",
                    json_response=is_control,
                )
            finally:
                connection.close()

        def do_GET(self) -> None:
            self._proxy()

        def do_HEAD(self) -> None:
            self._proxy()

        def do_POST(self) -> None:
            self._proxy()

        def do_OPTIONS(self) -> None:
            self._proxy()

    return GatewayHandler


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--bridge-network", default=DEFAULT_BRIDGE_NETWORK)
    parser.add_argument(
        "--bridge-token-file",
        default=DEFAULT_BRIDGE_TOKEN_FILE,
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.host not in {DEFAULT_HOST, "127.0.0.1", "localhost"}:
        raise SystemExit("gateway must bind to the Pi LAN or loopback address")
    server = ThreadingHTTPServer(
        (args.host, args.port),
        handler_class(
            bridge_network=args.bridge_network,
            bridge_token_file=args.bridge_token_file,
        ),
    )
    print(f"PiNAS cleanup gateway listening on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
