#!/usr/bin/env python3
"""Render NAS-specific systemd units from public deployment templates."""

from __future__ import annotations

import argparse
from pathlib import Path


UNIT_NAMES = (
    "pinas-storage-cleanup-control.service",
    "pinas-storage-cleanup-web.service",
    "pinas-storage-cleanup-gateway.service",
)


def render_systemd_unit(template: str, *, base: str, user: str, address: str) -> str:
    """Substitute deployment context without exposing credentials."""

    values = {
        "@PINAS_BASE@": base,
        "@PINAS_USER@": user,
        "@PINAS_ADDRESS@": address,
    }
    rendered = template
    for marker, value in values.items():
        rendered = rendered.replace(marker, value)
    if "@PINAS_" in rendered:
        raise ValueError("systemd 模板包含未渲染的 PiNAS 占位符。")
    return rendered


def render_directory(
    source_dir: Path,
    output_dir: Path,
    *,
    base: str,
    user: str,
    address: str,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for name in UNIT_NAMES:
        source = source_dir / name
        if not source.is_file():
            raise FileNotFoundError(source)
        rendered = render_systemd_unit(
            source.read_text(encoding="utf-8"),
            base=base,
            user=user,
            address=address,
        )
        (output_dir / name).write_text(rendered, encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--address", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    render_directory(
        args.source_dir,
        args.output_dir,
        base=args.base,
        user=args.user,
        address=args.address,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
