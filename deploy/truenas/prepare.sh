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

# TrueNAS names datasets without the /mnt/ prefix, and the template's header comments
# refer to them that way. Derive that form so the comments end up correct too.
DATASET_PATH="${BASE_PATH#/mnt/}"

# Order matters: the full mount path is substituted before the bare dataset form, so the
# more specific pattern wins and cannot be mangled by the looser one.
sed \
  -e "s|/mnt/<pool>/apps/fitness|$BASE_PATH|g" \
  -e "s|<pool>/apps/fitness|$DATASET_PATH|g" \
  -e "s|<nas-ip>|$NAS_IP|g" \
  -e "s|<truenas-ip>|$NAS_IP|g" \
  -e "s|CHANGEME_sidecar_password|$PASSWORD|g" \
  -e "s|\"8080:80\"|\"$WGER_PORT:80\"|g" \
  -e "s|\"8100:8000\"|\"$AGENT_PORT:8000\"|g" \
  -e "s|:20128/v1|:$GATEWAY_PORT/v1|g" \
  "$TEMPLATE" > "$OUTPUT"

# --- validate --------------------------------------------------------------------
problems=0

leftover="$(grep -nE '<pool>|<nas-ip>|<truenas-ip>|CHANGEME_sidecar_password' "$OUTPUT" || true)"
if [ -n "$leftover" ]; then
  warn "unsubstituted placeholders remain:"
  printf '%s\n' "$leftover"
  problems=$((problems + 1))
fi

# The password must appear exactly twice: the sidecar-db environment and the agent DSN.
# Any other count means the template changed and this script needs updating.
count="$(grep -c -- "$PASSWORD" "$OUTPUT" || true)"
if [ "$count" -ne 2 ]; then
  warn "expected the sidecar password in 2 places, found $count"
  problems=$((problems + 1))
fi

if ! grep -q "^  - path: $BASE_PATH/repo/wger/docker-compose.yml" "$OUTPUT"; then
  warn "the wger compose include path does not look right; check --base-path"
  problems=$((problems + 1))
fi

if command -v python3 >/dev/null 2>&1; then
  # !override is a Compose-specific YAML tag; strip it just for the syntax check.
  # Exit status 2 means PyYAML is unavailable, which is a skip rather than a failure.
  set +e
  python3 - "$OUTPUT" <<'YAMLCHECK'
import sys
try:
    import yaml
except ImportError:
    sys.exit(2)
text = open(sys.argv[1]).read().replace("!override", "")
try:
    doc = yaml.safe_load(text)
    top = sorted(doc)
    assert top == ["include", "name", "services"], f"unexpected top-level keys: {top}"
    services = sorted(doc["services"])
    for required in ("agent", "sidecar-db", "db", "nginx", "web"):
        assert required in services, f"missing service: {required}"
except Exception as exc:
    print(f"   {type(exc).__name__}: {exc}")
    sys.exit(1)
print("   YAML OK - services: " + ", ".join(services))
YAMLCHECK
  yaml_status=$?
  set -e
  if [ "$yaml_status" -eq 2 ]; then
    warn "PyYAML not installed; skipped structural validation of the generated YAML"
  elif [ "$yaml_status" -ne 0 ]; then
    warn "generated YAML failed validation"
    problems=$((problems + 1))
  fi
else
  warn "python3 not found; skipped YAML validation"
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
