# TrueNAS SCALE setup, step by step

Takes about 45 minutes, most of it waiting on image pulls and the exercise import.

**Steps 0-6 have been executed on a real TrueNAS SCALE box** (2026-07-30) and the whole
stack came up: wger, Postgres, redis, celery, PowerSync, the sidecar with its schema
loaded, and the agent reporting `{"status":"ok"}`. That run found six defects, all fixed
here — a Compose-only YAML tag TrueNAS rejects, a compose file generated on the wrong
machine, two config files the dataset ACL left unreadable, a missing PowerSync setup
command, and a mount path resolved against the wrong base directory.

**Steps 7-13 have not been executed.** They are written from wger's REST API and Django
source rather than from a working run, so expect at least one to need adjustment. The
checkpoints below tell you what "working" looks like at each stage, so a failure is
localized instead of mysterious.

Replace `<base-path>` with your dataset base (e.g. `/mnt/Nas/Apps/fitness`) and `<nas-ip>`
with your TrueNAS IP throughout. Step 6 generates a filled-in compose file for you.

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

Names are relative to the pool, and **case-sensitive** — whatever you use here must match
the `--base-path` you pass in step 6 exactly.

| Dataset (under your pool) | Holds | Snapshot priority |
|---|---|---|
| `Apps/fitness/wger-postgres` | Your training log, routines, body metrics | **High — irreplaceable** |
| `Apps/fitness/wger-media` | Exercise images | Medium |
| `Apps/fitness/wger-static` | Static assets | None needed |
| `Apps/fitness/sidecar-postgres` | Exercise data, profile, chat | Low — rebuildable |
| `Apps/fitness/repo` | This repo and wger's compose | Low — it's in git |

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
cd <base-path>/repo
git clone https://github.com/wger-project/docker.git wger
git clone <your-repo-url> ai-fitness
```

Then copy your spreadsheet into `ai-fitness/`. From your Mac:

```sh
scp "Functional+Fitness+Exercise+Database+(version+2.9).xlsx" \
    root@<nas-ip>:<base-path>/repo/ai-fitness/
```

**Checkpoint:** `ls <base-path>/repo/ai-fitness/` shows `agent/`, `etl/`,
`sidecar/`, and the `.xlsx`.

---

## Step 4 — Fix ownership and config-file readability

This is the single most common cause of a stack that starts and then dies. wger's images
run as UID/GID 1000; Postgres runs as 999.

```sh
cd <base-path>
chown -R 1000:1000 wger-media wger-static repo
chown -R 999:999   wger-postgres sidecar-postgres
```

Then make the bind-mounted config files world-readable:

```sh
chmod o+r  repo/wger/config/redis.conf \
           repo/wger/config/nginx.conf \
           repo/ai-fitness/sidecar/schema.sql
chmod -R o+rX repo/wger/services/config-powersync
```

This second part is easy to skip and fails confusingly. TrueNAS datasets inherit an ACL
that creates every new file `770` — even files git would normally write as `644` — so
`other` gets nothing. Three containers then cannot read a file they are handed:

| File | Read by | If unreadable |
|------|---------|---------------|
| `config/redis.conf` | `redis`, UID 999 | `redis-server` exits immediately; Compose reports `cache-1 is unhealthy` and the whole stack refuses to come up |
| `sidecar/schema.sql` | `postgres`, UID 999 | Schema never loads — and `sidecar-db` still reports **healthy**, because the cluster is created before the init scripts run. Recovering needs the data directory emptied, not just a restart. |
| `services/config-powersync/` | powersync's own user | powersync restart-loops |

Ownership alone is not enough here: these run as 999, not 1000, so being owned by
`1000:1000` does not help them. wger's own compose file says as much — *"they should be
readable by everyone."*

`config/prod.env` is deliberately **not** in that list, and you should not
`chmod -R o+r` the whole `config` directory. `prod.env` holds `SECRET_KEY` and your
database password, and it is consumed through `env_file:`, which the Docker daemon reads
as root — no container user ever needs it.

A `git pull` in `repo/wger` recreates files under the same inherited ACL, so re-run the
`chmod` after upgrading wger. `prepare.sh` checks this and prints the exact commands.

---

## Step 5 — Configure wger

```sh
cd <base-path>/repo/wger
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

Don't hand-edit the template — and don't paste `compose.yaml` itself. Generate a
flattened copy:

```sh
cd <base-path>/repo/ai-fitness
./deploy/truenas/prepare.sh --base-path <base-path> --ip <nas-ip>
```

For example, for a pool named `Nas` with datasets under `Apps/fitness`:

```sh
./deploy/truenas/prepare.sh --base-path /mnt/Nas/Apps/fitness --ip 192.168.0.199
```

Pass the **whole base path**, not just a pool name: dataset paths are case-sensitive, so
`/mnt/Nas/Apps/fitness` is not `/mnt/Nas/apps/fitness`.

This must run **on the TrueNAS box, after step 3**, because it reads wger's cloned
compose files.

### Why generated rather than hand-edited

TrueNAS validates the pasted YAML with a standard parser, which rejects Docker Compose's
own tags (`!override`, `!reset`) as unknown — you get `Invalid YAML provided`. That rules
out `include:`-ing wger's compose and overriding a few keys, because replacing a *list*
(the nginx port) without `!override` makes Compose **append**, leaving `80:80` in place
and colliding with the TrueNAS web UI.

So the generator resolves everything up front and emits one flat, tag-free file. It also
handles three things `sed` cannot:

- **Nine relative paths** across wger's compose files, each resolving relative to the
  file it appears in rather than one project root. Left alone, the stack starts and
  immediately dies on missing env files.
- **The `static` and `media` named volumes**, redirected onto datasets *everywhere* they
  are referenced. nginx mounts both to serve them and celery_worker writes to media —
  pointing only `web` at a bind mount leaves nginx serving an empty volume, so every
  static asset 404s while nothing looks obviously broken.
- **The sidecar password**, written to both places it is needed.

wger's clone is only ever read, never modified, so `git pull` there stays safe. Re-run
`prepare.sh` afterwards to pick up upstream changes.

It validates before declaring success: no leftover placeholders, no Compose tags, no
relative host paths, password in exactly two places, nginx not on port 80, and a **strict**
YAML parse with no tag stripping — the same thing TrueNAS does.

Options: `--wger-port`, `--agent-port`, `--gateway-port`, `--timezone`, `--password`.

Then:

1. **Save the printed password.** It is not shown again.
2. Confirm `LLM_BASE_URL` points at your gateway on the **NAS IP** — it is a separate
   TrueNAS app on a separate Docker network, so a service name will not resolve.
3. `WGER_API_TOKEN` is still `CHANGEME_wger_token`. Leave it; step 8 creates it.
4. **Apps → Discover Apps → Custom App → Install via YAML**. Paste the contents of
   **`deploy/truenas/compose.generated.yaml`**. Name the app `fitness`. Install.

**Checkpoint:** `docker ps` shows containers for wger's `web`, `db`, `nginx`, `cache`,
celery, plus `sidecar-db` and `agent` (OmniRoute appears too, from its own app). Find
their real names — TrueNAS prefixes them:

```sh
docker ps --format '{{.Names}}\t{{.Status}}'
```

Everything below uses `docker exec`, not `docker compose exec`, because TrueNAS owns the
compose project. Later steps spell the container names out literally — with the app named
`fitness` they are `ix-fitness-web-1`, `ix-fitness-agent-1`, `ix-fitness-db-1`,
`ix-fitness-sidecar-db-1`, `ix-fitness-cache-1` and `ix-fitness-powersync-1`. If you named
the app something else, substitute accordingly.

If a container is restarting, `docker logs <name> --tail 50` almost always names the
cause. Permissions (step 4) and `SECRET_KEY`/`SITE_URL` (step 5) cover most of it.

### Updating the agent after a `git pull`

The agent's Python is `COPY`ed into its image at build time, so pulling new code is not
enough — and TrueNAS's Stop/Start reuses the existing image rather than rebuilding it.
Force a rebuild:

```sh
cd <base-path>/repo/ai-fitness && git pull
docker rmi $(docker inspect --format '{{.Image}}' ix-fitness-agent-1)
```

Then Stop and Start the app in the UI; Compose rebuilds the missing image. The ETL and
loader scripts are exempt — they execute from the `/repo` bind mount, so they always run
whatever is checked out.

### Then create PowerSync's storage role

wger's stack needs one command after the containers start — it is the second half of
wger's own two-step TLDR, and nothing in the compose file does it for you:

```sh
docker exec ix-fitness-web-1 ./manage.py setup-powersync-storage
docker restart $(docker ps -a --format '{{.Names}}' | grep powersync | head -1)
```

`prod.env` already points PowerSync at
`postgres://powersync_storage:powersync_password@db:5432/wger`, but the role itself does
not exist until you run this. Skip it and the `powersync` container restart-loops on:

```
Fatal startup error - exiting with code 150.
password authentication failed for user "powersync_storage"
```

The SQL that creates that role lives in `dev-postgres/initdb/03-powersync.sql`, which only
the *development* compose stack mounts — so it never runs in production, and it is not an
initdb script you can add after the fact, because initdb only fires on an empty data
directory.

PowerSync backs offline mode in wger's mobile app. Nothing else in this stack depends on
it, so a restart-looping `powersync` container does not block the web UI, the import, or
the agent — you can carry on and fix it later if you only use the browser.

**Checkpoint:** `docker logs <powersync-container> --tail 5` no longer shows the auth
error.

---

## Step 7 — Load the exercise database

The ETL runs **inside the agent container** — the TrueNAS host has nowhere to
pip-install `openpyxl` that survives an OS upgrade.

Container names are literal below rather than shell variables. An unset variable here
silently shifts the arguments — `docker exec -w /repo $AGENT python x.py` with `$AGENT`
empty reports `No such container: python`, which reads like a broken image. TrueNAS names
containers `ix-<app-name>-<service>-1`; confirm yours with
`docker ps --format '{{.Names}}'`.

```sh
# 1. Extract and normalize the spreadsheet (~10 seconds)
docker exec -w /repo ix-fitness-agent-1 python etl/extract_custom_db.py

# 2. Read the data-quality report before loading anything
docker exec -w /repo ix-fitness-agent-1 cat build/qc_report.md | head -40

# 3. Load into the sidecar
docker exec -w /repo ix-fitness-agent-1 python sidecar/load.py --custom
```

These run against the repo **bind mount** at `/repo`, not the code baked into the image,
so a `git pull` takes effect without rebuilding. (The service code does not — see
"Updating the agent" in step 6.)

**Checkpoint:** step 1 prints `extracted 3242 exercises`; step 3 prints
`upserted 3242 custom exercises` and a summary reading
`ffed-2.9  3242 exercises  0 loggable in wger  3242 described`.

`0 loggable` is correct at this point — nothing is loggable until the wger import in
step 9.

Mirroring wger's own 828 exercises is **step 8**, not this step: it needs an API token,
which does not exist until you have registered.

---

## Step 8 — Create your wger account and API token

1. Open `http://<nas-ip>:8080` and register.
2. Go to `http://<nas-ip>:8080/en/user/api-key` and generate a token.
3. Put it in the compose YAML as `WGER_API_TOKEN`, then **Edit** the app in the TrueNAS UI
   and redeploy so the agent picks it up.

**Checkpoint:**

```sh
docker exec ix-fitness-agent-1 python -c "
from wger_client import WgerClient; print(WgerClient().check_connection())"
# {'ok': True, ...}
```

### Now mirror wger's own exercises

This needs the token, which is why it lives here rather than in step 7. `prod.env` ships
`ALLOW_GUEST_USERS=False`, so wger answers an unauthenticated read of
`/api/v2/exerciseinfo/` with **403** rather than serving it.

```sh
docker exec -w /repo ix-fitness-agent-1 python sidecar/load.py --wger --wger-url http://web:8000
```

If you would rather not redeploy first, pass the token directly instead — the loader
prefers the flag over the environment:

```sh
docker exec -w /repo ix-fitness-agent-1 \
  python sidecar/load.py --wger --wger-url http://web:8000 --wger-token <your-token>
```

**Checkpoint:** the summary shows roughly 3,242 `ffed-2.9` plus 828 `wger-upstream`.

```sh
curl -s http://<nas-ip>:8100/health
# {"status":"ok","exercises":4070}
```

A `degraded` status means the agent cannot reach `sidecar-db` — check the password
matches in both places in the compose file.

---

## Step 9 — Import the exercises into wger

Staged deliberately — 3,242 rows is not something to fire blind.

```sh
cd <base-path>/repo/ai-fitness

# Dry run: reports what would happen, writes nothing
docker exec -i -e DRY_RUN=1 ix-fitness-web-1 python3 manage.py shell \
  < wger_import/import_exercises.py

# 25 exercises, so you can eyeball them before committing
docker exec -i -e IMPORT_LIMIT=25 ix-fitness-web-1 python3 manage.py shell \
  < wger_import/import_exercises.py
```

Now **look at them in wger** — search for "Clubbell". Check the description rendered, and
that equipment and muscles look sane. This is the checkpoint that matters most; everything
downstream assumes the import is right.

```sh
# The rest (a few minutes)
docker exec -i ix-fitness-web-1 python3 manage.py shell < wger_import/import_exercises.py

# Refresh wger's cached exercise API so the mobile apps see them
docker exec ix-fitness-web-1 python3 manage.py warmup-exercise-api-cache --force
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
docker exec ix-fitness-agent-1 python -c "
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

Open the coach: **`http://<nas-ip>:8100/`**

Ask for a draft first, so nothing is written to wger until you have read it:

> Draft me a 4-day kettlebell and clubbell strength program, 60 minutes a session. Show me the draft before saving it.

The chat streams each step as it happens — which exercises it searched for, whether the
programming checks passed, what the reviewing coach said. Expect 30–90 seconds and
several model calls for a full program. Read the reasoning, then ask for changes in the
same conversation and finally tell it to save.

The coach decides for itself whether a message needs the full pipeline. Questions,
single-exercise substitutions and "is my posterior chain volume enough" are answered
directly, without the cost of a program build.

The same pipeline is also reachable directly, which is useful for scripting or when you
want the raw validator output:

```sh
curl -s -X POST http://<nas-ip>:8100/api/routine/generate \
  -H 'Content-Type: application/json' \
  -d '{"request":"4-day kettlebell and clubbell strength program","write":false}' \
  | python3 -m json.tool
```

That returns `plan.rationale`, `violations` and `critic` in full. Set `"write": true` to
save, then open the returned `routine_url`.

**This is where the untested code is most likely to break.** The config write path —
sets/reps/weight/RIR/rest and the progression rule — is my best reading of wger's schema,
which documents the fields but not how they interact. If sets or reps come out wrong in
wger's UI, that logic is isolated in `_write_entry` in `agent/wger_client.py`. A write
failure rolls the whole routine back, so a bad attempt cannot leave debris.

---

## Step 13 — Try a variation

Ask for something novel in the chat:

> Suggest two new clubbell core variations I could try.

Then open `http://<nas-ip>:8100/variations`, read each movement, and approve or reject.
Approved variations need one more import run to become loggable:

```sh
docker exec -i ix-fitness-web-1 python3 manage.py shell < wger_import/import_exercises.py
```

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| Container restart loop | Ownership (step 4). `docker logs <name> --tail 50`. |
| `dependency failed to start: container ix-fitness-cache-1 is unhealthy` | `config/redis.conf` is not world-readable, so redis (UID 999) cannot read it and exits at once — note it fails in the same second it starts, rather than timing out. `chmod o+r` it (step 4). |
| powersync: `password authentication failed for user "powersync_storage"` | The role is created by a management command, not by the compose file. Run `docker exec <web> ./manage.py setup-powersync-storage` (end of step 6), then restart the powersync container. Affects mobile offline sync only. |
| `/health` says `UndefinedTable: relation "exercises" does not exist`, but `sidecar-db` is **healthy** | `schema.sql` was unreadable on first start. The entrypoint creates the cluster *before* running `/docker-entrypoint-initdb.d/*`, so it initialized, died on the unreadable script, restarted, found a populated data directory, and reported healthy with no tables — it never retries. `chmod o+r` the file (step 4), stop the app, `sudo find <base-path>/sidecar-postgres -mindepth 1 -delete`, start it again. |
| CSRF error on any wger form | `SITE_URL` / `CSRF_TRUSTED_ORIGINS` don't match the URL you typed, including port. |
| `Invalid YAML provided` on install | You pasted `compose.yaml` instead of `compose.generated.yaml`. The template contains Compose's `!override` tag, which TrueNAS's YAML parser rejects. Run `prepare.sh` and paste its output. |
| App won't start, port conflict | nginx still on 80. Regenerate with `prepare.sh`; it asserts nginx is not on 80 before succeeding. |
| Containers die on missing env file | A relative path survived flattening. Regenerate with `prepare.sh` rather than hand-editing. |
| Static assets and exercise images 404 | nginx serving an empty named volume. Regenerate — the generator redirects `static`/`media` on every service that mounts them. |
| `/health` says `degraded` | Agent can't reach `sidecar-db`; check the password matches in both places. |
| `capabilities` shows tools dropped | That OmniRoute upstream provider doesn't support native tool calling. Switch to one listed as `native`. |
| Agent can't reach OmniRoute | Must be the NAS IP, not `http://omniroute:20128` — separate app, separate Docker network. Check OmniRoute's port is published on `0.0.0.0`. |
| Import: "cannot reach the agent service" | Agent container down, or `AGENT_URL` wrong. Inside wger the hostname is `agent`, not `localhost`. |
| Import: "no exercises this run has not already handled" | Link-back to the sidecar is failing. Check the agent's logs. |
| `load.py --wger` fails with 403 | Expected before step 8. `ALLOW_GUEST_USERS=False` means wger refuses anonymous reads of the exercise API; the mirror needs a real token. |
| Muscle names reported not found | Expected — ~12 muscles have no wger equivalent. Exercises still import. |
| Routine generation times out | Normal on first run; several model calls plus ~100 wger writes. |
| Chat answers but never searches or reads your profile | The gateway is dropping the tools array. Check `/capabilities` — a provider tiered `none` silently discards tools, so the coach answers from memory and looks like it is working. |
| Chat replies but the sidebar stays on "New conversation" | Cosmetic only; the title is set server-side from the first message and appears on reload. |

## After it works

Set up snapshot tasks: **Data Protection → Periodic Snapshot Tasks**. Aggressive on
`wger-postgres`, light on the rest. That database is the only thing here you cannot
regenerate.
