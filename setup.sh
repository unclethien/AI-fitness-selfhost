#!/usr/bin/env bash
#
# One-time setup. Idempotent — safe to re-run.
#
# Clones the official wger Docker stack into ./vendor/wger UNMODIFIED, so upstream
# upgrades are a plain `git pull` with nothing of ours to conflict with. Our services
# live in the root docker-compose.yml and attach to wger's network.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

WGER_DOCKER_REPO="https://github.com/wger-project/docker.git"
VENDOR_DIR="vendor/wger"

info() { printf '\033[1;34m==>\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$1"; }
fail() { printf '\033[1;31merror:\033[0m %s\n' "$1" >&2; exit 1; }

# --- prerequisites ------------------------------------------------------------
command -v git >/dev/null || fail "git is not installed"

if ! command -v docker >/dev/null; then
  fail "docker is not installed or not on PATH.

  This stack needs Docker with Compose v2. On macOS, install one of:
    brew install --cask docker        # Docker Desktop
    brew install --cask orbstack      # lighter, faster on Apple silicon
    brew install colima docker docker-compose && colima start

  Then re-run ./setup.sh"
fi

docker compose version >/dev/null 2>&1 || fail \
  "'docker compose' (v2) is unavailable. The legacy 'docker-compose' binary won't work:
  this project's compose file uses features from Compose v2."

docker info >/dev/null 2>&1 || fail "the Docker daemon isn't running — start it and re-run"

# --- vendor the wger stack ----------------------------------------------------
if [ -d "$VENDOR_DIR/.git" ]; then
  info "wger stack already present in $VENDOR_DIR (leaving it alone)"
  info "to update it later:  git -C $VENDOR_DIR pull"
else
  info "cloning the official wger Docker stack into $VENDOR_DIR"
  mkdir -p vendor
  git clone --depth 1 "$WGER_DOCKER_REPO" "$VENDOR_DIR"
fi

# --- secrets ------------------------------------------------------------------
random_secret() { python3 -c "import secrets; print(secrets.token_urlsafe(${1:-32}))"; }

if [ ! -f .env ]; then
  info "creating .env from .env.example"
  cp .env.example .env
  # Only the sidecar password is generated; API keys are the user's to supply.
  sidecar_pw="$(random_secret 32)"
  # BSD sed (macOS) needs the empty -i argument; GNU sed accepts it too via ''.
  if sed --version >/dev/null 2>&1; then
    sed -i "s|^SIDECAR_DB_PASSWORD=.*|SIDECAR_DB_PASSWORD=${sidecar_pw}|" .env
  else
    sed -i '' "s|^SIDECAR_DB_PASSWORD=.*|SIDECAR_DB_PASSWORD=${sidecar_pw}|" .env
  fi
  info "generated a sidecar database password"
else
  info ".env already exists (leaving it alone)"
fi

# wger keeps its own env file; give it a real SECRET_KEY rather than the shipped default,
# which regenerates on every restart and invalidates all sessions.
WGER_ENV="$VENDOR_DIR/config/prod.env"
if [ -f "$WGER_ENV" ] && grep -q 'SECRET_KEY=wger-docker-supersecret-key' "$WGER_ENV"; then
  info "replacing wger's placeholder SECRET_KEY with a generated one"
  wger_secret="$(random_secret 50)"
  if sed --version >/dev/null 2>&1; then
    sed -i "s|^SECRET_KEY=.*|SECRET_KEY=${wger_secret}|" "$WGER_ENV"
  else
    sed -i '' "s|^SECRET_KEY=.*|SECRET_KEY=${wger_secret}|" "$WGER_ENV"
  fi
fi

# --- python env for the ETL (runs on the host, not in a container) ------------
if [ ! -d .venv ]; then
  info "creating .venv for the ETL scripts"
  python3 -m venv .venv
fi
info "installing ETL dependencies"
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -r requirements.txt

cat <<'NEXT'

==> Setup complete. Remaining steps:

  1. Fill in .env:
       OPENROUTER_API_KEY   from https://openrouter.ai/keys
       WGER_API_TOKEN       created in step 3 below

  2. Start wger, then this project's services:
       docker compose -f vendor/wger/docker-compose.yml up -d
       docker compose up -d sidecar-db

  3. Create your wger account and API token:
       open http://localhost
       register, then visit http://localhost/en/user/api-key
       paste the token into .env as WGER_API_TOKEN

  4. Extract the spreadsheet and load both exercise sources:
       ./.venv/bin/python etl/extract_custom_db.py
       ./.venv/bin/python sidecar/load.py --custom
       ./.venv/bin/python sidecar/load.py --wger --wger-url http://localhost

  5. Review build/qc_report.md before importing into wger.

NEXT
