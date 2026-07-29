#!/opt/homebrew/bin/python3.12
"""Deploy one immutable read-only storage-cleanup release to the Pi."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import shlex
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PI_HOST = "nas-user@192.0.2.1"
PI_BASE = Path("/mnt/sdc/library-tools/storage-cleanup-ui")


def run(command: list[str], *, timeout: int = 900) -> None:
    subprocess.run(command, check=True, timeout=timeout)


def ssh_script(script: str, *, timeout: int = 900) -> None:
    completed = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", PI_HOST, "bash", "-se"],
        input=script,
        text=True,
        check=True,
        timeout=timeout,
    )
    if completed.returncode:
        raise RuntimeError("Pi deployment command failed")


def main() -> int:
    release_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    release = PI_BASE / "releases" / release_id
    shared_runtime = PI_BASE / "shared/runtime"
    shared_public = PI_BASE / "shared/public-data"

    ssh_script(
        f"""
set -euo pipefail
install -d -m 0755 {shlex.quote(str(PI_BASE))} {shlex.quote(str(PI_BASE / "releases"))} {shlex.quote(str(PI_BASE / "shared"))}
install -d -m 0700 {shlex.quote(str(shared_runtime))}
install -d -m 0755 {shlex.quote(str(shared_public))}
install -d -m 0755 {shlex.quote(str(release))}
test ! -e {shlex.quote(str(release / ".deployment-complete"))}
"""
    )

    run(
        [
            "rsync",
            "-a",
            "--exclude",
            "node_modules/",
            "--exclude",
            ".runtime/",
            "--exclude",
            "public/data/",
            "--exclude",
            "dist/",
            "--exclude",
            ".vinext/",
            "--exclude",
            ".wrangler/",
            "--exclude",
            "tsconfig.tsbuildinfo",
            f"{PROJECT_ROOT}/",
            f"{PI_HOST}:{release}/",
        ],
        timeout=300,
    )
    cache_files = (
        "hr-infohash-cache.json",
        "qb-file-cache.json",
        "media-metadata-cache.json",
    )
    for name in cache_files:
        source = PROJECT_ROOT / ".runtime" / name
        if source.is_file():
            run(
                [
                    "rsync",
                    "-a",
                    str(source),
                    f"{PI_HOST}:{shared_runtime}/{name}",
                ]
            )

    ssh_script(
        f"""
set -euo pipefail
release={shlex.quote(str(release))}
base={shlex.quote(str(PI_BASE))}
find {shlex.quote(str(shared_runtime))} -maxdepth 1 -type f -exec chmod 0600 {{}} +
rm -rf "$release/.runtime" "$release/public/data"
ln -s {shlex.quote(str(shared_runtime))} "$release/.runtime"
ln -s {shlex.quote(str(shared_public))} "$release/public/data"
cd "$release"
/usr/bin/npm ci --no-audit --no-fund --registry=https://registry.npmjs.org --replace-registry-host=never
/usr/bin/python3 -m unittest discover -s tests -p 'test_*.py'
/usr/bin/npm run lint
/usr/bin/npm run typecheck
/usr/bin/npm run build
/usr/bin/python3 scripts/collect-readonly-snapshot.py --local-nas
/usr/bin/python3 - <<'PY'
import json
from pathlib import Path
private = json.loads(Path(".runtime/resource-inventory.json").read_text())
public = json.loads(Path("public/data/resource-snapshot.json").read_text())
assert private["snapshotId"] == public["snapshotId"]
assert len(private["resources"]) == len(public["resources"])
PY
touch "$release/.deployment-complete"
next_link="$base/current.{release_id}.next"
ln -s "releases/{release_id}" "$next_link"
mv -Tf "$next_link" "$base/current"
sudo -n install -m 0644 "$release/deploy/systemd/pinas-storage-cleanup-control.service" /etc/systemd/system/
sudo -n install -m 0644 "$release/deploy/systemd/pinas-storage-cleanup-web.service" /etc/systemd/system/
sudo -n install -m 0644 "$release/deploy/systemd/pinas-storage-cleanup-gateway.service" /etc/systemd/system/
sudo -n systemctl daemon-reload
sudo -n systemctl enable pinas-storage-cleanup-control.service pinas-storage-cleanup-web.service pinas-storage-cleanup-gateway.service
sudo -n systemctl restart pinas-storage-cleanup-control.service pinas-storage-cleanup-web.service pinas-storage-cleanup-gateway.service
"""
    )

    ssh_script(
        f"""
set -euo pipefail
for attempt in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8765/health >/tmp/pinas-cleanup-health.json &&
     curl -fsS http://127.0.0.1:3001/ >/dev/null &&
     curl -fsS http://192.0.2.1:3000/ >/dev/null &&
     curl -fsS http://192.0.2.1:3000/control/health >/tmp/pinas-cleanup-gateway-health.json; then
    break
  fi
  sleep 1
done
/usr/bin/python3 {shlex.quote(str(release / "scripts/smoke-readonly.py"))} --expect-execution-enabled
/usr/bin/python3 - <<'PY'
import json
from pathlib import Path
health = json.loads(Path("/tmp/pinas-cleanup-health.json").read_text())
gateway_health = json.loads(Path("/tmp/pinas-cleanup-gateway-health.json").read_text())
assert health["ok"] is True
assert health["executionEnabled"] is True
assert health["inventoryCurrent"] is True
assert health["runtimeMode"] == "pi-local"
assert health["hostName"] == "raspberrypi"
assert gateway_health == health
PY
sudo -n systemctl is-enabled pinas-storage-cleanup-control.service pinas-storage-cleanup-web.service pinas-storage-cleanup-gateway.service
sudo -n systemctl is-active pinas-storage-cleanup-control.service pinas-storage-cleanup-web.service pinas-storage-cleanup-gateway.service
"""
    )
    print(
        json.dumps(
            {
                "ok": True,
                "release": release_id,
                "piBase": str(PI_BASE),
                "mode": "read-only",
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
