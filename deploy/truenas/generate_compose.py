#!/usr/bin/env python3
"""Flatten wger's compose stack plus the AI layer into one plain-YAML file.

TrueNAS validates a custom app's YAML with a standard parser, which rejects Docker
Compose's own tags (`!override`, `!reset`) as unknown — the paste fails with
"Invalid YAML provided". That rules out the obvious approach of `include:`-ing wger's
compose and overriding a few keys, because overriding a *list* (the nginx port) without
`!override` makes Compose append rather than replace, leaving "80:80" in place and
colliding with the TrueNAS web UI.

So this resolves everything ahead of time and emits one flat, tag-free file:

  1. Loads wger's docker-compose.yml and follows its `include:` list.
  2. Rewrites every relative path to absolute. There are nine of them across those
     files, and each resolves relative to the file it appears in, not to a single
     project root — `services/postgres.yaml` says `../config/prod.env` while
     `services/powersync.yaml` says `./config-powersync`. Flattening without fixing
     these produces a stack that starts and immediately fails on missing env files.
  3. Applies our overrides with explicit replace semantics, in Python, where we control
     the merge instead of inferring Compose's.
  4. Appends the sidecar database and agent.
  5. Prunes the top-level `volumes:` map to the named volumes still referenced after
     our bind mounts replaced several of them.

wger's own files are only ever READ. Nothing is written into its clone, so upstream
upgrades remain a plain `git pull` and re-running this picks up their changes.
"""

from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit(
        "error: PyYAML is required.\n"
        "  On TrueNAS run this inside a container that has it, or: pip install --user pyyaml"
    )

def load_with_includes(compose_path: Path) -> dict:
    """Load a compose file and its `include:` tree into one document.

    Relative paths are made absolute HERE, per source file, before any merging. A single
    service can be defined in one file and extended in another -- `cache` comes from
    services/redis.yaml (`../config/redis.conf`) and is extended in docker-compose.yml
    (`redis-data:/data`) -- so the two contributions have different base directories.
    Resolving after the merge, against one guessed directory, silently produces a path
    one level off and a container that fails on a missing config file.
    """
    document = yaml.safe_load(compose_path.read_text()) or {}
    merged: dict = {"services": {}, "volumes": {}, "networks": {}}

    def absorb(doc: dict, source: Path) -> None:
        for name, service in (doc.get("services") or {}).items():
            resolved = absolutize_service(service, source)
            if name in merged["services"]:
                # Later definitions layer onto earlier ones, which is how wger's own
                # docker-compose.yml adds logging and volumes to services defined in
                # services/*.yaml.
                merged["services"][name] = deep_merge(merged["services"][name], resolved)
            else:
                merged["services"][name] = resolved
        for key in ("volumes", "networks"):
            for vname, value in (doc.get(key) or {}).items():
                merged[key][vname] = copy.deepcopy(value)

    # Includes are processed first so the including file's own definitions layer on top.
    for entry in document.get("include") or []:
        included_path = entry["path"] if isinstance(entry, dict) else entry
        resolved_path = (compose_path.parent / included_path).resolve()
        if not resolved_path.exists():
            sys.exit(
                f"error: {compose_path.name} includes {included_path}, "
                f"not found at {resolved_path}"
            )
        absorb(yaml.safe_load(resolved_path.read_text()) or {}, resolved_path)

    absorb(document, compose_path)
    return merged


def deep_merge(base: dict, overlay: dict) -> dict:
    """Merge overlay onto base. Lists are concatenated, matching Compose's own rules for
    the include case this is emulating."""
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        elif key in result and isinstance(result[key], list) and isinstance(value, list):
            result[key] = result[key] + [v for v in value if v not in result[key]]
        else:
            result[key] = copy.deepcopy(value)
    return result


def absolutize(value: str, base_dir: Path) -> str:
    """Turn a relative host path into an absolute one, preserving a bind mount's
    ':container:mode' suffix."""
    if value.startswith("/") or value.startswith("$"):
        return value
    if not (value.startswith("./") or value.startswith("../")):
        # A bare name is a named volume, not a path.
        return value
    parts = value.split(":")
    parts[0] = str((base_dir / parts[0]).resolve())
    return ":".join(parts)


def absolutize_service(service: dict, source_file: Path) -> dict:
    base_dir = source_file.parent
    service = copy.deepcopy(service)

    env_file = service.get("env_file")
    if isinstance(env_file, str):
        service["env_file"] = absolutize(env_file, base_dir)
    elif isinstance(env_file, list):
        service["env_file"] = [
            absolutize(e, base_dir) if isinstance(e, str) else e for e in env_file
        ]

    volumes = service.get("volumes")
    if isinstance(volumes, list):
        service["volumes"] = [
            absolutize(v, base_dir) if isinstance(v, str) else v for v in volumes
        ]

    build = service.get("build")
    if isinstance(build, str):
        service["build"] = absolutize(build, base_dir)
    elif isinstance(build, dict) and isinstance(build.get("context"), str):
        build["context"] = absolutize(build["context"], base_dir)

    return service


def used_named_volumes(services: dict) -> set[str]:
    """Named volumes still referenced, after bind mounts replaced several."""
    used: set[str] = set()
    for service in services.values():
        for entry in service.get("volumes") or []:
            if isinstance(entry, str):
                source = entry.split(":")[0]
                if not source.startswith("/") and not source.startswith("."):
                    used.add(source)
            elif isinstance(entry, dict) and entry.get("type") == "volume":
                if entry.get("source"):
                    used.add(entry["source"])
    return used


def build_compose(
    wger_repo: Path,
    repo: Path,
    base_path: Path,
    nas_ip: str,
    password: str,
    timezone: str,
    wger_port: int,
    agent_port: int,
    gateway_port: int,
    app_name: str,
) -> dict:
    compose_path = wger_repo / "docker-compose.yml"
    if not compose_path.exists():
        sys.exit(
            f"error: wger's compose not found at {compose_path}\n"
            "  Clone it first (setup step 3):\n"
            f"    git clone https://github.com/wger-project/docker.git {wger_repo}"
        )

    merged = load_with_includes(compose_path)
    services: dict = merged["services"]

    # --- our overrides, replace semantics ------------------------------------
    if "nginx" not in services:
        sys.exit("error: wger's compose has no 'nginx' service; the port remap needs updating")
    # TrueNAS's web UI owns 80/443, so wger moves rather than the NAS UI.
    services["nginx"]["ports"] = [f"{wger_port}:80"]

    if "db" not in services:
        sys.exit("error: wger's compose has no 'db' service")
    services["db"]["volumes"] = [
        f"{base_path}/wger-postgres:/var/lib/postgresql/data"
    ]
    # wger hardcodes TZ=Europe/Berlin here, and an explicit environment entry beats
    # env_file, so prod.env alone does not win.
    services["db"]["environment"] = [f"TZ={timezone}"]

    if "web" not in services:
        sys.exit("error: wger's compose has no 'web' service")

    # Redirect the `static` and `media` named volumes onto ZFS datasets EVERYWHERE they
    # are referenced, not just on `web`. nginx mounts both read-only to serve them and
    # celery_worker mounts media to write generated files; pointing only `web` at a bind
    # mount would leave those services reading an empty named volume, and every static
    # asset would 404 while nothing obvious looked broken.
    volume_redirects = {
        "static": f"{base_path}/wger-static",
        "media": f"{base_path}/wger-media",
        "postgres-data": f"{base_path}/wger-postgres",
    }
    for service in services.values():
        entries = service.get("volumes")
        if not isinstance(entries, list):
            continue
        rewritten = []
        for entry in entries:
            if isinstance(entry, str):
                parts = entry.split(":")
                if parts[0] in volume_redirects:
                    parts[0] = volume_redirects[parts[0]]
                    entry = ":".join(parts)
            rewritten.append(entry)
        service["volumes"] = rewritten

    # --- AI layer -------------------------------------------------------------
    dsn = f"postgresql://fitness:{password}@sidecar-db:5432/exercise_intel"
    services["sidecar-db"] = {
        "image": "docker.io/postgres:17-alpine",
        "environment": {
            "POSTGRES_DB": "exercise_intel",
            "POSTGRES_USER": "fitness",
            "POSTGRES_PASSWORD": password,
            "TZ": timezone,
        },
        # Sets the Postgres server's timezone, not just the container OS: CURRENT_DATE
        # drives the contraindication-expiry filter.
        "command": ["postgres", "-c", f"timezone={timezone}"],
        "volumes": [
            f"{base_path}/sidecar-postgres:/var/lib/postgresql/data",
            f"{repo}/sidecar/schema.sql:/docker-entrypoint-initdb.d/01-schema.sql:ro",
        ],
        "healthcheck": {
            "test": ["CMD-SHELL", "pg_isready -U fitness -d exercise_intel"],
            "interval": "10s",
            "timeout": "5s",
            "retries": 5,
            "start_period": "30s",
        },
        "restart": "unless-stopped",
    }

    services["agent"] = {
        "build": {"context": str(repo), "dockerfile": "agent/Dockerfile"},
        "depends_on": {"sidecar-db": {"condition": "service_healthy"}},
        "environment": {
            "SIDECAR_DSN": dsn,
            # Same Compose project now, so wger's service name resolves.
            "WGER_BASE_URL": "http://web:8000",
            "WGER_API_TOKEN": "CHANGEME_wger_token",
            # The LLM gateway is a separate TrueNAS app on a separate Docker network,
            # so it is addressed by host IP rather than service name.
            "LLM_BASE_URL": f"http://{nas_ip}:{gateway_port}/v1",
            "LLM_API_KEY": "local-gateway",
            "MODEL_ROUTINE": "anthropic/claude-sonnet-5",
            "MODEL_ROUTINE_ESCALATION": "anthropic/claude-opus-5",
            "MODEL_VARIATION": "anthropic/claude-sonnet-5",
            "MODEL_CRITIC": "anthropic/claude-sonnet-5",
            "TZ": timezone,
        },
        # Mounted read-write so the ETL can read the .xlsx and write build/.
        "volumes": [f"{repo}:/repo"],
        "ports": [f"{agent_port}:8000"],
        "healthcheck": {
            "test": [
                "CMD", "python", "-c",
                "import urllib.request; urllib.request.urlopen"
                "('http://localhost:8000/health')",
            ],
            "interval": "15s",
            "timeout": "5s",
            "retries": 5,
            "start_period": "40s",
        },
        "restart": "unless-stopped",
    }

    # --- top level -----------------------------------------------------------
    compose: dict = {"name": app_name, "services": services}

    still_used = used_named_volumes(services)
    volumes = {
        name: value for name, value in (merged["volumes"] or {}).items()
        if name in still_used
    }
    # Volumes referenced but never declared must still be declared, or Compose errors.
    for name in still_used:
        volumes.setdefault(name, None)
    if volumes:
        compose["volumes"] = volumes

    if merged["networks"]:
        compose["networks"] = merged["networks"]

    return compose


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-path", required=True,
                       help="dataset base, e.g. /mnt/Nas/Apps/fitness")
    parser.add_argument("--ip", required=True, help="this TrueNAS box's LAN IP")
    parser.add_argument("--password", required=True, help="sidecar database password")
    parser.add_argument("--timezone", default="America/Chicago")
    parser.add_argument("--wger-port", type=int, default=8080)
    parser.add_argument("--agent-port", type=int, default=8100)
    parser.add_argument("--gateway-port", type=int, default=20128)
    parser.add_argument("--app-name", default="fitness")
    parser.add_argument("--wger-repo", default=None,
                       help="default <base-path>/repo/wger")
    parser.add_argument("--repo", default=None,
                       help="default <base-path>/repo/ai-fitness")
    parser.add_argument("-o", "--output", required=True)
    args = parser.parse_args()

    base_path = Path(args.base_path.rstrip("/"))
    wger_repo = Path(args.wger_repo) if args.wger_repo else base_path / "repo" / "wger"
    repo = Path(args.repo) if args.repo else base_path / "repo" / "ai-fitness"

    compose = build_compose(
        wger_repo=wger_repo,
        repo=repo,
        base_path=base_path,
        nas_ip=args.ip,
        password=args.password,
        timezone=args.timezone,
        wger_port=args.wger_port,
        agent_port=args.agent_port,
        gateway_port=args.gateway_port,
        app_name=args.app_name,
    )

    header = f"""\
# GENERATED FILE -- do not edit by hand; re-run deploy/truenas/prepare.sh instead.
#
# Flattened from wger's own compose stack plus the AI layer, with every relative path
# made absolute and no Compose-specific YAML tags, because TrueNAS validates this with a
# standard YAML parser that rejects them.
#
# wger's clone at {wger_repo} was only read, never modified,
# so `git pull` there stays safe. Re-run prepare.sh afterwards to pick up its changes.
#
#   wger      http://{args.ip}:{args.wger_port}
#   agent     http://{args.ip}:{args.agent_port}
#   gateway   http://{args.ip}:{args.gateway_port}/v1
#
# WGER_API_TOKEN is still a placeholder -- create it in setup step 8, then edit the app.
"""
    body = yaml.safe_dump(compose, sort_keys=False, default_flow_style=False, width=100)
    Path(args.output).write_text(header + body)
    print(f"   services: {', '.join(sorted(compose['services']))}")
    if compose.get("volumes"):
        print(f"   named volumes kept: {', '.join(sorted(compose['volumes']))}")


if __name__ == "__main__":
    main()
