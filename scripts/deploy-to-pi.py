#!/opt/homebrew/bin/python3.12
"""Deploy one immutable cleanup release and its MoviePilot native plugin."""

from __future__ import annotations

from datetime import datetime, timezone
import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PI_HOST = "nas-user@nas-host"
PI_BASE = Path("/srv/storage-cleanup-ui")


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


def pi_address() -> str:
    return PI_HOST.rsplit("@", 1)[-1]


def pi_user() -> str:
    if "@" not in PI_HOST:
        raise ValueError("--pi-host 必须包含 user@host，才能渲染 systemd 服务用户。")
    user = PI_HOST.rsplit("@", 1)[0].strip()
    if not user or any(character.isspace() for character in user):
        raise ValueError("--pi-host 的 SSH 用户无效。")
    return user


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pi-host",
        default=os.environ.get("PINAS_PI_HOST", PI_HOST),
        help="SSH target, or PINAS_PI_HOST.",
    )
    parser.add_argument(
        "--pi-base",
        type=Path,
        default=Path(os.environ.get("PINAS_PI_BASE", str(PI_BASE))),
        help="Persistent deployment directory, or PINAS_PI_BASE.",
    )
    return parser.parse_args()


def main() -> int:
    global PI_HOST, PI_BASE
    args = parse_args()
    PI_HOST = args.pi_host
    PI_BASE = args.pi_base
    service_user = pi_user()
    plugin_manifest = json.loads(
        (PROJECT_ROOT / "package.v2.json").read_text(
            encoding="utf-8"
        )
    )
    plugin_version = str(plugin_manifest["StorageCleanup"]["version"])
    plugin_remote_path = f"dist/v{plugin_version}/assets/remoteEntry.js"
    if not plugin_version or any(
        character not in "0123456789." for character in plugin_version
    ):
        raise ValueError("StorageCleanup 插件版本号无效。")
    release_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    release = PI_BASE / "releases" / release_id
    shared_runtime = PI_BASE / "shared/runtime"
    shared_public = PI_BASE / "shared/public-data"
    shared_config = PI_BASE / "shared/config.json"

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
            "/dist/",
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
        "hr-source-cache.json",
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
cd "$release/plugins.v2/storagecleanup"
/usr/bin/npm ci --no-audit --no-fund --registry=https://registry.npmjs.org --replace-registry-host=never
/usr/bin/npm run check
/usr/bin/npm run build
test -s {shlex.quote(plugin_remote_path)}
cd "$release"
/usr/bin/python3 scripts/collect-readonly-snapshot.py --local-nas --config {shlex.quote(str(shared_config))}
/usr/bin/python3 - <<'PY'
import json
from pathlib import Path
private = json.loads(Path(".runtime/resource-inventory.json").read_text())
public = json.loads(Path("public/data/resource-snapshot.json").read_text())
assert private["snapshotId"] == public["snapshotId"]
assert len(private["resources"]) == len(public["resources"])
PY
/usr/bin/python3 scripts/render_systemd_units.py \\
  --source-dir "$release/deploy/systemd" \\
  --output-dir "$release/.rendered-systemd" \\
  --base {shlex.quote(str(PI_BASE))} \\
  --user {shlex.quote(service_user)} \\
  --address {shlex.quote(pi_address())}
touch "$release/.deployment-complete"
next_link="$base/current.{release_id}.next"
ln -s "releases/{release_id}" "$next_link"
mv -Tf "$next_link" "$base/current"
sudo -n install -m 0644 "$release/.rendered-systemd/pinas-storage-cleanup-control.service" /etc/systemd/system/
sudo -n install -m 0644 "$release/.rendered-systemd/pinas-storage-cleanup-web.service" /etc/systemd/system/
sudo -n install -m 0644 "$release/.rendered-systemd/pinas-storage-cleanup-gateway.service" /etc/systemd/system/
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
     curl -fsS http://{shlex.quote(pi_address())}:3000/ >/dev/null &&
     curl -fsS http://{shlex.quote(pi_address())}:3000/control/health >/tmp/pinas-cleanup-gateway-health.json; then
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

    ssh_script(
        f"""
set -euo pipefail
sudo -n docker exec -i moviepilot-v2-pilot /opt/venv/bin/python3 - <<'PY'
import json
from pathlib import Path

from app.core.config import settings
from app.db.systemconfig_oper import SystemConfigOper
from app.helper.plugin import PluginHelper
from app.schemas.types import SystemConfigKey

plugin_id = "StorageCleanup"
repo_path = Path({str(PI_BASE / "current")!r})
if not (repo_path / "package.v2.json").is_file():
    raise SystemExit("MoviePilot local plugin manifest is missing")

updated, message = settings.update_setting("PLUGIN_LOCAL_REPO_PATHS", str(repo_path))
if updated is False:
    raise SystemExit(f"failed to configure local plugin repository: {{message}}")

helper = PluginHelper()
repo_url = helper.make_local_repo_url(plugin_id, repo_path, "v2")
installed, message = helper.install(
    pid=plugin_id,
    repo_url=repo_url,
    force_install=True,
)
if not installed:
    raise SystemExit(f"failed to install local plugin: {{message}}")

config = SystemConfigOper()
installed_plugins = config.get(SystemConfigKey.UserInstalledPlugins) or []
if plugin_id not in installed_plugins:
    installed_plugins.append(plugin_id)
    config.set(SystemConfigKey.UserInstalledPlugins, installed_plugins)

print(json.dumps({{
    "ok": True,
    "plugin": plugin_id,
    "installed": installed,
    "localRepo": str(repo_path),
}}, ensure_ascii=False))
PY
sudo -n docker restart moviepilot-v2-pilot >/dev/null
ready_streak=0
for attempt in $(seq 1 180); do
  health="$(sudo -n docker inspect moviepilot-v2-pilot --format '{{{{if .State.Health}}}}{{{{.State.Health.Status}}}}{{{{else}}}}none{{{{end}}}}' 2>/dev/null || true)"
  if [ "$health" = "healthy" ] &&
     curl -fsS "http://127.0.0.1:3101/api/v1/system/global?token=moviepilot" >/dev/null; then
    ready_streak=$((ready_streak + 1))
    if [ "$ready_streak" -ge 2 ]; then
      break
    fi
  else
    ready_streak=0
  fi
  sleep 2
done
test "$ready_streak" -ge 2
curl -fsS "http://127.0.0.1:3101/api/v1/system/global?token=moviepilot" >/dev/null
test "$(sudo -n docker inspect moviepilot-v2-pilot --format '{{{{.State.Health.Status}}}}')" = "healthy"
/usr/bin/python3 - <<'PY'
import json
from urllib.request import urlopen

with urlopen(
    "http://127.0.0.1:3101/api/v1/plugin/remotes?token=moviepilot",
    timeout=15,
) as response:
    remotes = json.load(response)
remote = next(
    (item for item in remotes if item.get("id") == "StorageCleanup"),
    None,
)
assert remote
assert str(remote.get("url") or "").endswith(
    "/{plugin_remote_path}"
)
print(json.dumps({{
    "ok": True,
    "plugin": "StorageCleanup",
    "remote": remote["url"],
}}, ensure_ascii=False))
PY
token="$(cat {shlex.quote(str(shared_runtime / "control-token"))})"
sudo -n docker exec \
  -e PINAS_BRIDGE_TOKEN="$token" \
  moviepilot-v2-pilot \
  sh -lc 'curl -fsS -H "X-PiNAS-Bridge-Token: $PINAS_BRIDGE_TOKEN" -H "X-PiNAS-Session: $PINAS_BRIDGE_TOKEN" http://{shlex.quote(pi_address())}:3000/control/health' \
  >/tmp/pinas-cleanup-moviepilot-bridge-health.json
/usr/bin/python3 - <<'PY'
import json
from pathlib import Path
payload = json.loads(
    Path("/tmp/pinas-cleanup-moviepilot-bridge-health.json").read_text()
)
assert payload["ok"] is True
assert payload["executionEnabled"] is True
print(json.dumps({{
    "ok": True,
    "moviePilotBridge": True,
    "executionEnabled": payload["executionEnabled"],
}}, ensure_ascii=False))
PY
"""
    )
    print(
        json.dumps(
            {
                "ok": True,
                "release": release_id,
                "piBase": str(PI_BASE),
                "mode": "execution-enabled",
                "moviePilotPlugin": "StorageCleanup",
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
