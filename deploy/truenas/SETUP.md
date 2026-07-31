# TrueNAS SCALE setup, step by step

Takes about 45 minutes, most of it waiting on image pulls and the exercise import.

**Nothing in this document has been executed.** It is written from TrueNAS and wger
documentation, not from a working run. Expect at least one step to need adjustment; the
checkpoints below tell you what "working" looks like at each stage so a failure is
localized instead of mysterious.

Replace `<pool>` with your pool name and `<nas-ip>` with your TrueNAS IP throughout.

**Assumes OmniRoute is already installed and running** on this TrueNAS as its own app,
reachable at `http://<nas-ip>:20128`.

---

## Step 0 — Check your TrueNAS version

Docker-backed custom apps need **SCALE 24.10 (Electric Eel) or newer**. Earlier SCALE
used Kubernetes and this compose file will not work. TrueNAS **CORE** is FreeBSD, has no
Docker at all, and would need a Linux VM instead.

In the UI: **System → Update** shows the version.

---

## Step 1 — Create the datasets

**Storage → Datasets**, select your pool, then **Add Dataset** for each of these. Defaults
are fine for every field.

| Dataset | Holds | Snapshot priority |
|---|---|---|
| `apps/fitness/wger-postgres` | Your training log, routines, body metrics | **High — irreplaceable** |
| `apps/fitness/wger-media` | Exercise images | Medium |
| `apps/fitness/wger-static` | Static assets | None needed |
| `apps/fitness/sidecar-postgres` | Exercise data, profile, chat | Low — rebuildable |
| `apps/fitness/repo` | This repo and wger's compose | Low — it's in git |

OmniRoute keeps its own storage under its own app; nothing to create for it here.

Worth setting these snapshot policies differently. **Your wger database is the only
irreplaceable data in the stack** — the sidecar rebuilds from the spreadsheet in about a
minute, so don't spend retention on it.

---

## Step 2 — Get a shell

**System Settings → Shell** in the UI, or SSH in. Everything from here is command line.

---

## Step 3 — Clone both repos

```sh
cd /mnt/<pool>/apps/fitness/repo
git clone https://github.com/wger-project/docker.git wger
git clone <your-repo-url> ai-fitness
```

Then copy your spreadsheet into `ai-fitness/`. From your Mac:

```sh
scp "Functional+Fitness+Exercise+Database+(version+2.9).xlsx" \
    root@<nas-ip>:/mnt/<pool>/apps/fitness/repo/ai-fitness/
```

**Checkpoint:** `ls /mnt/<pool>/apps/fitness/repo/ai-fitness/` shows `agent/`, `etl/`,
`sidecar/`, and the `.xlsx`.

---

## Step 4 — Fix ownership

This is the single most common cause of a stack that starts and then dies. wger's images
run as UID/GID 1000; Postgres runs as 999.

```sh
cd /mnt/<pool>/apps/fitness
chown -R 1000:1000 wger-media wger-static repo
chown -R 999:999   wger-postgres sidecar-postgres
```

---

## Step 5 — Configure wger

```sh
cd /mnt/<pool>/apps/fitness/repo/wger
python3 -c "import secrets; print(secrets.token_urlsafe(50))"   # copy the output
nano config/prod.env
```

Set these four:

```ini
SECRET_KEY=<the string you just generated>
SITE_URL=http://<nas-ip>:8080
CSRF_TRUSTED_ORIGINS=http://<nas-ip>:8080
TIME_ZONE=America/Chicago
TZ=America/Chicago
```

`SITE_URL` and `CSRF_TRUSTED_ORIGINS` **must match the address you actually type in the
browser**, including the port. If they don't, every form submission in wger fails with a
CSRF error and nothing tells you why.

`TIME_ZONE` and `TZ` must both be `America/Chicago`, and must match the `TZ` set on the
`sidecar-db` and `agent` services in the compose file. This is not cosmetic: America/Chicago
is UTC-5/-6, so a container left on UTC dates every workout logged after about 6pm
local to the *next day*. Routine start dates and the recent-training lookback window
both use the container's idea of today.

Note the port is **8080, not 80** — TrueNAS's own web UI owns 80 and 443. The compose file
remaps wger accordingly. Don't move the NAS UI instead; that risks locking you out.

---

## Step 6 — Install the app

1. Open the compose file and replace every `<pool>` and `<nas-ip>`:
   ```sh
   sed -i 's|<pool>|YOURPOOL|g; s|<nas-ip>|192.168.1.50|g' \
       /mnt/<pool>/apps/fitness/repo/ai-fitness/deploy/truenas/compose.yaml
   ```
2. Set `CHANGEME_sidecar_password` — the **same value in both places** it appears
   (the `sidecar-db` environment and the agent's `SIDECAR_DSN`). Leave
   `CHANGEME_wger_token` alone; you create it in step 8.
3. Confirm `LLM_BASE_URL` points at your existing OmniRoute:
   `http://<nas-ip>:20128/v1`. It must be the **NAS IP, not `http://omniroute:20128`** —
   OmniRoute is a separate TrueNAS app, so it is a separate Compose project on a separate
   Docker network and the service name will not resolve from this app.
4. In the UI: **Apps → Discover Apps → Custom App → Install via YAML**. Paste the whole
   file. Name the app `fitness`. Install.

**Checkpoint:** `docker ps` shows containers for wger's `web`, `db`, `nginx`, `cache`,
celery, plus `sidecar-db` and `agent` (OmniRoute appears too, from its own app). Find
their real names — TrueNAS prefixes them:

```sh
docker ps --format '{{.Names}}\t{{.Status}}'
```

Save the names you'll need. Everything below uses `docker exec`, not
`docker compose exec`, because TrueNAS owns the compose project:

```sh
WGER=$(docker ps --format '{{.Names}}' | grep -E 'web' | head -1)
AGENT=$(docker ps --format '{{.Names}}' | grep -E 'agent' | head -1)
echo "wger=$WGER agent=$AGENT"
```

If a container is restarting, `docker logs <name> --tail 50` almost always names the
cause. Permissions (step 4) and `SECRET_KEY`/`SITE_URL` (step 5) cover most of it.

---

## Step 7 — Load the exercise database

The ETL runs **inside the agent container** — the TrueNAS host has nowhere to
pip-install `openpyxl` that survives an OS upgrade.

```sh
# 1. Extract and normalize the spreadsheet (~10 seconds)
docker exec -w /repo "$AGENT" python etl/extract_custom_db.py

# 2. Read the data-quality report before loading anything
docker exec -w /repo "$AGENT" cat build/qc_report.md | head -40

# 3. Load into the sidecar
docker exec -w /repo "$AGENT" python sidecar/load.py --custom

# 4. Mirror wger's own 828 exercises into the same schema
docker exec -w /repo "$AGENT" python sidecar/load.py --wger --wger-url http://web:8000
```

**Checkpoint:** step 1 prints `extracted 3242 exercises`, and step 4's summary shows
roughly 3,242 `ffed-2.9` plus 828 `wger-upstream`. Then:

```sh
curl -s http://<nas-ip>:8100/health
# {"status":"ok","exercises":4070}
```

A `degraded` status here means the agent cannot reach `sidecar-db` — check the password
matches in both places in the compose file.

---

## Step 8 — Create your wger account and API token

1. Open `http://<nas-ip>:8080` and register.
2. Go to `http://<nas-ip>:8080/en/user/api-key` and generate a token.
3. Put it in the compose YAML as `WGER_API_TOKEN`, then **Edit** the app in the TrueNAS UI
   and redeploy so the agent picks it up.

**Checkpoint:**

```sh
docker exec "$AGENT" python -c "
from wger_client import WgerClient; print(WgerClient().check_connection())"
# {'ok': True, ...}
```

---

## Step 9 — Import the exercises into wger

Staged deliberately — 3,242 rows is not something to fire blind.

```sh
cd /mnt/<pool>/apps/fitness/repo/ai-fitness

# Dry run: reports what would happen, writes nothing
docker exec -i -e DRY_RUN=1 "$WGER" python3 manage.py shell \
  < wger_import/import_exercises.py

# 25 exercises, so you can eyeball them before committing
docker exec -i -e IMPORT_LIMIT=25 "$WGER" python3 manage.py shell \
  < wger_import/import_exercises.py
```

Now **look at them in wger** — search for "Clubbell". Check the description rendered, and
that equipment and muscles look sane. This is the checkpoint that matters most; everything
downstream assumes the import is right.

```sh
# The rest (a few minutes)
docker exec -i "$WGER" python3 manage.py shell < wger_import/import_exercises.py

# Refresh wger's cached exercise API so the mobile apps see them
docker exec "$WGER" python3 manage.py warmup-exercise-api-cache --force
```

**Checkpoint:** `curl -s http://<nas-ip>:8100/api/import/status` shows `linked` equal to
`total` for `ffed-2.9`.

⚠️ **Never run `sync-exercises` with a delete flag.** Imported exercises carry
project-local UUIDs that are absent from upstream's deletion log. Ordinary syncing is
fine, and re-running the import restores anything lost anyway.

---

## Step 10 — Point at your OmniRoute and verify tool calling

OmniRoute is already running, so this is just wiring plus one important check.

**First confirm the agent can actually reach it.** Container-to-host networking is the
thing most likely to be wrong here:

```sh
docker exec "$AGENT" python -c "
import urllib.request, json
url = 'http://<nas-ip>:20128/v1/models'
print(json.loads(urllib.request.urlopen(url, timeout=10).read())['data'][:3])"
```

If that times out or refuses, the agent container cannot see the NAS IP. Check OmniRoute's
port is published on `0.0.0.0` rather than bound to `127.0.0.1` only.

**Then set the model IDs.** Note the exact IDs OmniRoute advertises — from the command
above, or its dashboard at `http://<nas-ip>:20128/dashboard`. **Do not assume OpenRouter's
`provider/model` naming carries over.** Put the real IDs in the compose YAML
(`MODEL_ROUTINE`, `MODEL_ROUTINE_ESCALATION`, `MODEL_VARIATION`, `MODEL_CRITIC`) and
redeploy the app.

**Checkpoint — the most important one in this document:**

```sh
curl -s http://<nas-ip>:8100/capabilities | python3 -m json.tool
```

You need `"ready": true` and `"native_tool_calling": true`.

OmniRoute classifies its upstream providers as `native`, `emulated`, or `none` — and
`none` **silently drops the tools array**. If you see
`"silently dropping the tools array"`, that provider cannot be used: the agent would
appear to work and never search your exercise database. Switch to a provider listed as
`native`. An `emulated` provider gets a brittleness warning; it may work but the nested
routine payload is exactly the kind of thing regex-parsed tool blocks get wrong.

`"json_schema_response_format": false` is fine — the client falls back to a forced tool
call automatically.

**Also worth deciding now:** running the gateway locally does *not* make inference local.
Unless you point OmniRoute at a local backend (Ollama / LM Studio / vLLM), your profile —
age, injuries, goals — and your training logs still leave the box to whichever upstream
provider you select. Your 5060 Ti closes that loop through the same gateway with no code
change on this side.

## Step 11 — Fill in your profile

`http://<nas-ip>:8100/profile`

Goals with priorities, birth year, experience level, sessions per week, minutes per
session, equipment you actually own, and any injuries or movements to avoid.

The equipment list is populated from your real exercise database, so it will show
Clubbell, Macebell, Sliders and the rest. Tick only what you have — anything unticked is
excluded outright.

---

## Step 12 — Generate your first routine

Dry run first: full pipeline, nothing written to wger.

```sh
curl -s -X POST http://<nas-ip>:8100/api/routine/generate \
  -H 'Content-Type: application/json' \
  -d '{"request":"4-day kettlebell and clubbell strength program","write":false}' \
  | python3 -m json.tool
```

Read `plan.rationale`, `violations` and `critic`. Expect this to take 30–90 seconds and
several model calls.

Then, for real:

```sh
curl -s -X POST http://<nas-ip>:8100/api/routine/generate \
  -H 'Content-Type: application/json' \
  -d '{"request":"4-day kettlebell and clubbell strength program","write":true}' \
  | python3 -m json.tool
```

Open the returned `routine_url` in wger.

**This is where the untested code is most likely to break.** The config write path —
sets/reps/weight/RIR/rest and the progression rule — is my best reading of wger's schema,
which documents the fields but not how they interact. If sets or reps come out wrong in
wger's UI, that logic is isolated in `_write_entry` in `agent/wger_client.py`. A write
failure rolls the whole routine back, so a bad attempt cannot leave debris.

---

## Step 13 — Try a variation

Ask for something novel, then review it:

```sh
curl -s -X POST http://<nas-ip>:8100/api/routine/generate \
  -H 'Content-Type: application/json' \
  -d '{"request":"Suggest two new clubbell core variations I could try","write":false}'
```

Then open `http://<nas-ip>:8100/variations`, read each movement, and approve or reject.
Approved variations need one more import run to become loggable:

```sh
docker exec -i "$WGER" python3 manage.py shell < wger_import/import_exercises.py
```

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| Container restart loop | Ownership (step 4). `docker logs <name> --tail 50`. |
| CSRF error on any wger form | `SITE_URL` / `CSRF_TRUSTED_ORIGINS` don't match the URL you typed, including port. |
| App won't start, port conflict | wger's nginx still on 80. The `!override` tag on `ports` is required — Compose *appends* to lists otherwise. |
| `/health` says `degraded` | Agent can't reach `sidecar-db`; check the password matches in both places. |
| `capabilities` shows tools dropped | That OmniRoute upstream provider doesn't support native tool calling. Switch to one listed as `native`. |
| Agent can't reach OmniRoute | Must be the NAS IP, not `http://omniroute:20128` — separate app, separate Docker network. Check OmniRoute's port is published on `0.0.0.0`. |
| Import: "cannot reach the agent service" | Agent container down, or `AGENT_URL` wrong. Inside wger the hostname is `agent`, not `localhost`. |
| Import: "no exercises this run has not already handled" | Link-back to the sidecar is failing. Check the agent's logs. |
| Muscle names reported not found | Expected — ~12 muscles have no wger equivalent. Exercises still import. |
| Routine generation times out | Normal on first run; several model calls plus ~100 wger writes. |

## After it works

Set up snapshot tasks: **Data Protection → Periodic Snapshot Tasks**. Aggressive on
`wger-postgres`, light on the rest. That database is the only thing here you cannot
regenerate.
