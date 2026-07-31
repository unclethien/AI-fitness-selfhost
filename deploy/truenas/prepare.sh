#!/usr/bin/env bash
#
# Produce a ready-to-paste TrueNAS custom-app YAML from the template.
#
#   ./deploy/truenas/prepare.sh --base-path /mnt/Nas/Apps/fitness --ip 192.168.0.199
#
# Substitutes the whole base path rather than just a pool name, because dataset paths
# are case-sensitive and layouts vary (/mnt/Nas/Apps/... is not /mnt/Nas/apps/...).
# Generates the sidecar database password once and writes it to BOTH places it is
# needed, which is otherwise easy to get wrong and fails as an opaque auth error.
#
# Output: deploy/truenas/compose.generated.yaml (gitignored — it contains a password).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEMPLATE="$REPO_ROOT/deploy/truenas/compose.yaml"
OUTPUT="$REPO_ROOT/deploy/truenas/compose.generated.yaml"

BASE_PATH=""
NAS_IP=""
WGER_PORT="8080"
AGENT_PORT="8100"
GATEWAY_PORT="20128"
TIMEZONE="America/Chicago"

info() { printf '\033[1;34m==>\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$1"; }
fail() { printf '\033[1;31merror:\033[0m %s\n' "$1" >&2; exit 1; }

usage() {
  cat <<'USAGE'
usage: prepare.sh --base-path <path> --ip <address> [options]

required:
  --base-path PATH   Dataset base, e.g. /mnt/Nas/Apps/fitness
                     (the parent of wger-postgres, sidecar-postgres, repo, ...)
  --ip ADDRESS       This TrueNAS box's LAN IP, e.g. 192.168.0.199

options:
  --wger-port N      Port to expose wger on          (default 8080)
  --agent-port N     Port to expose the agent on     (default 8100)
  --gateway-port N   Port your LLM gateway listens on (default 20128)
  --timezone TZ      IANA timezone                    (default America/Chicago)
  --password VALUE   Use this sidecar DB password instead of generating one
USAGE
}

PASSWORD=""
while [ $# -gt 0 ]; do
  case "$1" in
    --base-path) BASE_PATH="${2:-}"; shift 2 ;;
    --ip) NAS_IP="${2:-}"; shift 2 ;;
    --wger-port) WGER_PORT="${2:-}"; shift 2 ;;
    --agent-port) AGENT_PORT="${2:-}"; shift 2 ;;
    --gateway-port) GATEWAY_PORT="${2:-}"; shift 2 ;;
    --timezone) TIMEZONE="${2:-}"; shift 2 ;;
    --password) PASSWORD="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) fail "unknown argument: $1 (see --help)" ;;
  esac
done

[ -n "$BASE_PATH" ] || { usage; fail "--base-path is required"; }
[ -n "$NAS_IP" ] || { usage; fail "--ip is required"; }
[ -f "$TEMPLATE" ] || fail "template not found at $TEMPLATE"

# Trailing slash would produce doubled separators in every mount path.
BASE_PATH="${BASE_PATH%/}"

case "$BASE_PATH" in
  /*) ;;
  *) fail "--base-path must be absolute, got '$BASE_PATH'" ;;
esac

# A hostname would work for LLM_BASE_URL but not for container-to-host routing in
# every setup, so warn rather than silently accept something unroutable.
if ! printf '%s' "$NAS_IP" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'; then
  warn "--ip '$NAS_IP' is not a dotted IPv4 address; make sure containers can route to it"
fi

if [ -z "$PASSWORD" ]; then
  # Alphanumeric only, deliberately. The password is interpolated into a DSN
  # (postgresql://fitness:PASSWORD@sidecar-db:5432/...), where @ : / ? # would be
  # parsed as URL syntax and produce a confusing connection failure.
  #
  # Avoid `tr < /dev/urandom | head` here: head closes the pipe, tr takes SIGPIPE, and
  # under `set -o pipefail` that aborts the script with status 141.
  if command -v python3 >/dev/null 2>&1; then
    PASSWORD="$(python3 -c "
import secrets, string
alphabet = string.ascii_letters + string.digits
print(''.join(secrets.choice(alphabet) for _ in range(32)))")"
  elif command -v openssl >/dev/null 2>&1; then
    PASSWORD="$(openssl rand -hex 16)"
  else
    fail "need python3 or openssl to generate a password, or pass --password"
  fi
fi
case "$PASSWORD" in
  *[!A-Za-z0-9]*) fail "password must be alphanumeric only (it goes into a DSN URL)" ;;
esac

if [ -e "$OUTPUT" ]; then
  warn "$OUTPUT already exists and will be overwritten"
fi

info "base path   $BASE_PATH"
info "NAS IP      $NAS_IP"
info "wger        http://$NAS_IP:$WGER_PORT"
info "agent       http://$NAS_IP:$AGENT_PORT"
info "LLM gateway http://$NAS_IP:$GATEWAY_PORT/v1"
info "timezone    $TIMEZONE"

# Delegate to the generator: it flattens wger's compose stack, absolutizes every
# relative path against the file it came from, applies our overrides with explicit
# replace semantics, and emits plain tag-free YAML. sed cannot do any of that, and the
# `!override` tag the template used is rejected by TrueNAS's YAML parser.
WGER_REPO="${WGER_REPO:-$BASE_PATH/repo/wger}"
AI_REPO="${AI_REPO:-$BASE_PATH/repo/ai-fitness}"

if [ ! -f "$WGER_REPO/docker-compose.yml" ]; then
  fail "wger's compose not found at $WGER_REPO/docker-compose.yml

  This must run on the TrueNAS box, after setup step 3:
    cd $BASE_PATH/repo
    git clone https://github.com/wger-project/docker.git wger

  Override the location with WGER_REPO=/path/to/wger if it lives elsewhere."
fi

GENERATOR="$REPO_ROOT/deploy/truenas/generate_compose.py"
[ -f "$GENERATOR" ] || fail "generator not found at $GENERATOR"

GEN_ARGS=(
  --base-path "$BASE_PATH"
  --ip "$NAS_IP"
  --password "$PASSWORD"
  --timezone "$TIMEZONE"
  --wger-port "$WGER_PORT"
  --agent-port "$AGENT_PORT"
  --gateway-port "$GATEWAY_PORT"
  --wger-repo "$WGER_REPO"
  --repo "$AI_REPO"
  --output "$OUTPUT"
)

# The generator needs PyYAML to parse wger's compose. TrueNAS's host python may not have
# it, and pip-installing on the host does not survive an OS upgrade -- so fall back to a
# throwaway container. Host paths are mounted at the SAME absolute paths inside it, which
# matters because the generator resolves and existence-checks those paths.
if command -v python3 >/dev/null 2>&1 && python3 -c "import yaml" >/dev/null 2>&1; then
  info "generating with host python3"
  python3 "$GENERATOR" "${GEN_ARGS[@]}" || fail "generating the compose file failed"
elif command -v docker >/dev/null 2>&1; then
  info "host python3 lacks PyYAML; generating inside a throwaway container"
  DOCKER_MOUNTS=(-v "/mnt:/mnt")
  case "$REPO_ROOT" in
    /mnt/*) ;;
    *) DOCKER_MOUNTS+=(-v "$REPO_ROOT:$REPO_ROOT") ;;
  esac
  case "$WGER_REPO" in
    /mnt/*) ;;
    *) DOCKER_MOUNTS+=(-v "$WGER_REPO:$WGER_REPO") ;;
  esac
  docker run --rm "${DOCKER_MOUNTS[@]}" -w "$REPO_ROOT" \
    docker.io/python:3.12-slim \
    sh -c "pip install --quiet --no-cache-dir pyyaml && python3 '$GENERATOR' $(printf "'%s' " "${GEN_ARGS[@]}")" \
    || fail "generating the compose file inside a container failed"
else
  fail "need either python3 with PyYAML, or docker.

  On TrueNAS, docker is present, so this should not happen. Otherwise:
    pip install --user pyyaml"
fi

# --- validate --------------------------------------------------------------------
problems=0

leftover="$(grep -nE '<pool>|<nas-ip>|<truenas-ip>|CHANGEME_sidecar_password' "$OUTPUT" || true)"
if [ -n "$leftover" ]; then
  warn "unsubstituted placeholders remain:"
  printf '%s\n' "$leftover"
  problems=$((problems + 1))
fi

# Compose-specific YAML tags are exactly what TrueNAS rejects with "Invalid YAML
# provided", so assert the output is free of them.
if grep -qE '!override|!reset' "$OUTPUT"; then
  warn "output contains a Compose-specific YAML tag; TrueNAS will reject it"
  problems=$((problems + 1))
fi

# Any surviving relative host path would resolve against an unknown working directory.
if grep -qE '^\s+- \.{1,2}/' "$OUTPUT"; then
  warn "output still contains relative host paths:"
  grep -nE '^\s+- \.{1,2}/' "$OUTPUT"
  problems=$((problems + 1))
fi

# A bind-mounted config file must be readable by the UID the container runs as. TrueNAS
# datasets inherit an ACL that creates every file 770, so `other` gets nothing -- and the
# services that read these files run as 999, not as the 1000 that owns them. Owning them
# correctly is not enough. Symptom without this check: redis exits the instant it starts
# and TrueNAS reports only `container ix-<app>-cache-1 is unhealthy`, which points at the
# healthcheck rather than at a permission error.
#
# Deliberately a fixed list rather than every path in the generated file: the Postgres
# data directories are correctly 700 and owned by their container's UID, so a generic
# "is it world-readable" sweep would flag them and teach you to ignore the warning.
other_can_read() {
  local path="$1" mode other
  # BSD stat (macOS, for generating during development) takes different flags than GNU.
  mode="$(stat -c '%a' "$path" 2>/dev/null || stat -f '%Lp' "$path" 2>/dev/null)" || return 1
  other="${mode: -1}"
  [ $(( other & 4 )) -ne 0 ] || return 1
  # A directory also needs the execute bit before anything inside it can be reached.
  if [ -d "$path" ]; then [ $(( other & 1 )) -ne 0 ] || return 1; fi
  return 0
}

# Confirmed to break the stack: both are read by a process running as UID 999.
blocking_unreadable=()
for path in "$WGER_REPO/config/redis.conf" "$AI_REPO/sidecar/schema.sql"; do
  [ -e "$path" ] || continue
  other_can_read "$path" || blocking_unreadable+=("$path")
done

# Same mechanism, but the reading UID depends on the image, so advise rather than block.
advisory_unreadable=()
for path in "$WGER_REPO/config/nginx.conf" "$WGER_REPO/services/config-powersync"; do
  [ -e "$path" ] || continue
  other_can_read "$path" || advisory_unreadable+=("$path")
done

if [ "${#blocking_unreadable[@]}" -gt 0 ]; then
  warn "these files are not readable by the UID that has to read them (999):"
  for path in "${blocking_unreadable[@]}"; do
    printf '    %s  (mode %s)\n' "$path" \
      "$(stat -c '%a' "$path" 2>/dev/null || stat -f '%Lp' "$path" 2>/dev/null)"
  done
  printf '\n  Fix, then re-run:\n    chmod o+r%s' \
    "$(printf ' %s' "${blocking_unreadable[@]}")"
  printf '\n\n  Do NOT chmod the whole config directory: prod.env holds SECRET_KEY and the\n'
  printf '  database password, and only the root Docker daemon reads it.\n\n'
  problems=$((problems + 1))
fi

if [ "${#advisory_unreadable[@]}" -gt 0 ]; then
  warn "these may also need to be world-readable, depending on the image's user:"
  for path in "${advisory_unreadable[@]}"; do
    printf '    chmod -R o+rX %s\n' "$path"
  done
fi

count="$(grep -c -- "$PASSWORD" "$OUTPUT" || true)"
if [ "$count" -ne 2 ]; then
  warn "expected the sidecar password in 2 places, found $count"
  problems=$((problems + 1))
fi

# Parse with a STRICT parser -- no tag stripping -- because that is what TrueNAS does.
set +e
python3 - "$OUTPUT" <<'YAMLCHECK'
import sys
try:
    import yaml
except ImportError:
    sys.exit(2)
try:
    doc = yaml.safe_load(open(sys.argv[1]).read())
    assert "services" in doc, "no services key"
    services = sorted(doc["services"])
    for required in ("agent", "sidecar-db", "db", "nginx", "web"):
        assert required in services, f"missing service: {required}"
    ports = doc["services"]["nginx"].get("ports") or []
    assert not any(str(p).startswith("80:") for p in ports), \
        f"nginx still publishes port 80, which collides with the TrueNAS UI: {ports}"
except Exception as exc:
    print(f"   {type(exc).__name__}: {exc}")
    sys.exit(1)
print("   strict YAML parse OK - services: " + ", ".join(services))
YAMLCHECK
yaml_status=$?
set -e
if [ "$yaml_status" -eq 2 ]; then
  warn "PyYAML not installed; could not verify the output parses. Install it before"
  warn "pasting into TrueNAS: pip install --user pyyaml"
  problems=$((problems + 1))
elif [ "$yaml_status" -ne 0 ]; then
  warn "generated YAML failed validation"
  problems=$((problems + 1))
fi

echo
if [ "$problems" -gt 0 ]; then
  fail "$problems problem(s) found in $OUTPUT — do not install it until they are resolved"
fi

info "wrote $OUTPUT"
echo
cat <<NEXT
Sidecar database password (store it; you will not be shown it again):

    $PASSWORD

Remaining manual step in the YAML: WGER_API_TOKEN is still CHANGEME_wger_token.
You create that in step 8, after registering in wger, then edit the app and redeploy.

Next:
  1. Create the datasets under $BASE_PATH (step 1) if you have not already.
  2. chown them (step 4).
  3. Set SECRET_KEY / SITE_URL / CSRF_TRUSTED_ORIGINS / TIME_ZONE in
     $BASE_PATH/repo/wger/config/prod.env  (step 5).
  4. Paste $OUTPUT into
     Apps -> Discover Apps -> Custom App -> Install via YAML, name it "fitness".
NEXT
