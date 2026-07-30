# Importing the custom exercise database into wger

`import_exercises.py` creates your 3,242 exercises inside wger so they can be logged in
the web UI and the mobile apps.

## Why it works this way

**It does not modify wger.** No Django app registered, no `INSTALLED_APPS` change, no
volume mounted into the wger container, no fork. The script is piped into
`manage.py shell`, where Django is already configured, so there is no settings module to
locate either.

**It does not use the REST API.** `POST /api/v2/exercise/` treats `uuid` as read-only,
and this import depends on setting a deterministic project-local UUIDv5 per exercise so
that re-running *updates* rather than duplicates. Only the ORM can set that.

**It talks HTTP, not SQL.** The script fetches from the agent service rather than the
sidecar database, so it needs nothing beyond the Python standard library — it does not
matter which database driver the wger image happens to ship.

**Taxonomy is exchanged by name, not by id.** wger's category, muscle and equipment ids
come from fixtures and are not guaranteed identical across installations.

## Prerequisites

1. Sidecar loaded: `python etl/extract_custom_db.py && python sidecar/load.py --custom`
2. wger running, and the agent container running and reachable from it
3. A wger user account created (the exercises themselves are global, but you want an
   account to log against)

## Run it

Always dry-run first — it reports exactly what would happen and writes nothing:

```sh
docker compose exec -T -e DRY_RUN=1 web python3 manage.py shell \
  < wger_import/import_exercises.py
```

Then a small trial batch, so you can inspect the result in wger's UI before committing
to 3,242 rows:

```sh
docker compose exec -T -e IMPORT_LIMIT=25 web python3 manage.py shell \
  < wger_import/import_exercises.py
```

Look at a few in wger (Exercises → search for e.g. "Clubbell"), check the description
renders and the equipment/muscles look right, then run the full import:

```sh
docker compose exec -T web python3 manage.py shell < wger_import/import_exercises.py
```

Finally refresh wger's cached exercise API so the mobile apps see the new exercises:

```sh
docker compose exec web python3 manage.py warmup-exercise-api-cache --force
```

On TrueNAS SCALE, substitute the app's compose file:
`docker compose -p fitness exec -T web python3 manage.py shell < …`

## Options

| Env var | Default | Effect |
|---|---|---|
| `DRY_RUN` | `0` | `1` = report only, write nothing |
| `IMPORT_LIMIT` | `0` | Stop after N exercises |
| `IMPORT_ALL` | `0` | `1` = also re-push already-imported exercises (use after re-running the ETL to update descriptions or taxonomy) |
| `AGENT_URL` | `http://agent:8000` | Where to fetch exercise data from |

## What it does

1. **Equipment** — creates the 26 equipment types the custom database uses and wger
   lacks (Clubbell, Macebell, Sliders, Gymnastic Rings, Sandbag, Tire, Sled…).
   Idempotent by name; equipment rows are name-only so this is low-risk.
2. **Exercises** — `update_or_create` on the project-local UUID, setting category and the
   mapped muscles/equipment.
3. **Translations** — one English translation per exercise with the deterministically
   generated `description_source`. wger's `Translation.save()` renders that Markdown into
   its read-only `description` HTML field.
4. **Link-back** — writes each new wger exercise id into the sidecar, completing the
   cross-link so the agent knows which exercises are loggable.

Each exercise is committed in its own transaction, so one bad row cannot leave a
half-built exercise behind or abort the run.

## Re-running

Safe and idempotent. The UUID is derived from a project-local namespace plus the
exercise slug, so:

- re-running updates in place rather than duplicating
- generated UUIDs can never collide with an upstream wger.de exercise UUID
- after re-running the ETL, `IMPORT_ALL=1` pushes the updated descriptions

## ⚠️ Never run `sync-exercises` with a delete flag

Imported exercises carry project-local UUIDs that do not appear in upstream's deletion
log. Ordinary syncing is fine — it only adds and updates upstream exercises — but a
delete-enabled sync is not something this import can protect you from. Re-running the
import restores anything lost.

## Troubleshooting

**"cannot reach the agent service"** — the agent container is not running or `AGENT_URL`
is wrong. From inside the wger container the service name is `agent`, not `localhost`.

**"wger has no exercise category named X"** — the mapping in
`etl/mappings/wger_mappings.json` references a category this wger install does not have.
The command prints the categories it does know.

**"page contained no exercises this run has not already handled"** — the link-back to the
sidecar is failing, so exercises keep reappearing as pending. Check the agent's logs;
the script stops rather than looping.

**Muscle names listed as not found** — expected. About 12 muscles in the custom database
(iliopsoas, erector spinae, adductor magnus, the rotator cuff group…) have no wger
equivalent. The exercise still imports; the sidecar retains the precise muscle for the
agent to reason about.
