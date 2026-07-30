"""Import the custom exercise database into wger.

Runs INSIDE the wger container, piped into Django's shell so Django is already
configured — no settings module to locate, no app to add to INSTALLED_APPS, nothing
mounted, wger completely unmodified:

    docker compose exec -T web python3 manage.py shell < wger_import/import_exercises.py

Environment (all optional, defaults suit the compose setup):

    AGENT_URL      default http://agent:8000    where to fetch exercises from
    IMPORT_LIMIT   default 0 (no limit)         stop after N exercises, for a trial run
    IMPORT_ALL     default 0                    1 = also re-push already-linked exercises
    DRY_RUN        default 0                    1 = change nothing, just report

Uses only the Python standard library plus Django, so it does not care what the wger
image happens to have installed.

Why a script rather than the REST API: `POST /api/v2/exercise/` treats `uuid` as
read-only, and this import depends on setting a deterministic project-local UUIDv5 so
re-running updates rather than duplicates. Only the ORM can do that.
"""

import json
import os
import sys
import urllib.error
import urllib.request

# Django — already configured by `manage.py shell`.
from django.db import transaction

from wger.core.models import Language
from wger.exercises.models import (
    Equipment,
    Exercise,
    ExerciseCategory,
    Muscle,
    Translation,
)
from wger.utils.constants import ENGLISH_SHORT_NAME

AGENT_URL = os.environ.get("AGENT_URL", "http://agent:8000").rstrip("/")
IMPORT_LIMIT = int(os.environ.get("IMPORT_LIMIT", "0"))
IMPORT_ALL = os.environ.get("IMPORT_ALL", "0") == "1"
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
PAGE_SIZE = 250


def log(message):
    # print + flush, because output through `manage.py shell` is buffered otherwise and
    # a long import would look like it had hung.
    print(message)
    sys.stdout.flush()


def fetch(path):
    try:
        with urllib.request.urlopen(f"{AGENT_URL}{path}", timeout=120) as response:
            return json.loads(response.read().decode())
    except urllib.error.URLError as exc:
        log(f"ERROR: cannot reach the agent service at {AGENT_URL}{path}: {exc}")
        log("  Is the agent container running, and is AGENT_URL correct?")
        raise SystemExit(1)


def post(path, payload):
    request = urllib.request.Request(
        f"{AGENT_URL}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read().decode())


# ---------------------------------------------------------------------------
# Step 1 — equipment
# ---------------------------------------------------------------------------

def ensure_equipment():
    """Create the equipment rows the custom database needs and wger lacks.

    Idempotent by name. Equipment rows carry only a name (no images), so adding them is
    low-risk and does not affect wger's own exercises.
    """
    wanted = fetch("/api/equipment/to-create")["names"]
    existing = set(Equipment.objects.values_list("name", flat=True))
    missing = [name for name in wanted if name not in existing]

    if not missing:
        log(f"equipment: all {len(wanted)} custom types already present")
        return

    if DRY_RUN:
        log(f"equipment: would create {len(missing)}: {', '.join(missing)}")
        return

    # wger's Equipment.name is max_length=50; nothing in the list is close, but a future
    # database version might be.
    too_long = [n for n in missing if len(n) > 50]
    if too_long:
        log(f"ERROR: equipment names exceed wger's 50-char limit: {too_long}")
        raise SystemExit(1)

    Equipment.objects.bulk_create(
        [Equipment(name=name) for name in missing], ignore_conflicts=True
    )
    log(f"equipment: created {len(missing)} — {', '.join(missing)}")


# ---------------------------------------------------------------------------
# Step 2 — exercises
# ---------------------------------------------------------------------------

class Resolver:
    """Caches name -> wger object lookups.

    Taxonomy is exchanged by name because wger's fixture ids are not guaranteed
    identical across installations. A missing category or muscle is a hard error (the
    mapping is wrong); missing equipment is created.
    """

    def __init__(self):
        self.categories = {c.name: c for c in ExerciseCategory.objects.all()}
        self.muscles = {m.name: m for m in Muscle.objects.all()}
        self.equipment = {e.name: e for e in Equipment.objects.all()}
        self.unknown_muscles = set()

    def category(self, name):
        found = self.categories.get(name)
        if found is None:
            log(f"ERROR: wger has no exercise category named {name!r}. "
                f"Known: {sorted(self.categories)}")
            raise SystemExit(1)
        return found

    def muscle_list(self, names):
        out = []
        for name in names:
            found = self.muscles.get(name)
            if found is None:
                # Not fatal: the mapping file deliberately maps ~12 muscles to nothing,
                # and a name mismatch should not abort a 3,000-exercise import.
                self.unknown_muscles.add(name)
                continue
            out.append(found)
        return out

    def equipment_list(self, names):
        out = []
        for name in names:
            found = self.equipment.get(name)
            if found is None:
                if DRY_RUN:
                    continue
                found, _ = Equipment.objects.get_or_create(name=name[:50])
                self.equipment[name] = found
            out.append(found)
        return out


def import_exercises():
    english = Language.objects.get(short_name=ENGLISH_SHORT_NAME)
    resolver = Resolver()

    created = updated = skipped = 0
    links = []
    all_skipped = []
    offset = 0
    # Every uuid this run has already handled. With only_pending=true the offset stays
    # at 0 because processed rows leave the result set — so if a page ever comes back
    # containing nothing new (link-back failed, a row is permanently unprocessable), the
    # loop would otherwise repeat that page forever.
    seen_uuids = set()

    while True:
        page = fetch(
            f"/api/exercises/for-import?limit={PAGE_SIZE}&offset={offset}"
            f"&only_pending={'false' if IMPORT_ALL else 'true'}"
        )
        if offset == 0:
            log(f"exercises: {page['total_matching']} to process"
                f"{' (dry run)' if DRY_RUN else ''}")
        license_author = page["license_author"]
        all_skipped.extend(page.get("skipped") or [])

        items = page["items"]
        if not items:
            break

        fresh = [i for i in items if i["uuid"] not in seen_uuids]
        if not fresh:
            log(f"WARNING: page at offset {offset} contained no exercises this run has "
                "not already handled — stopping to avoid an endless loop.")
            log("  Usually means the link-back to the sidecar is failing, so exercises "
                "keep reappearing as pending. Check the agent's logs.")
            break
        seen_uuids.update(i["uuid"] for i in fresh)
        items = fresh

        for item in items:
            if IMPORT_LIMIT and (created + updated) >= IMPORT_LIMIT:
                log(f"reached IMPORT_LIMIT={IMPORT_LIMIT}, stopping")
                items = []
                break

            if DRY_RUN:
                exists = Exercise.objects.filter(uuid=item["uuid"]).exists()
                if exists:
                    updated += 1
                else:
                    created += 1
                continue

            try:
                # One transaction per exercise: a single bad row cannot leave a
                # half-built exercise behind, and the run can continue.
                with transaction.atomic():
                    exercise, was_created = Exercise.objects.update_or_create(
                        uuid=item["uuid"],
                        defaults={
                            "category": resolver.category(item["category"]),
                            "license_author": license_author,
                        },
                    )
                    exercise.muscles.set(resolver.muscle_list(item["muscles"]))
                    exercise.muscles_secondary.set(
                        resolver.muscle_list(item["muscles_secondary"])
                    )
                    exercise.equipment.set(resolver.equipment_list(item["equipment"]))

                    # description_source is what we write; wger's Translation.save()
                    # renders it to the read-only `description` HTML field itself.
                    Translation.objects.update_or_create(
                        exercise=exercise,
                        language=english,
                        defaults={
                            "name": item["name"],
                            "description_source": item["description_source"],
                            "license_author": license_author,
                        },
                    )
            except Exception as exc:  # noqa: BLE001
                skipped += 1
                all_skipped.append({
                    "uuid": item["uuid"],
                    "name": item["name"],
                    "reason": f"{type(exc).__name__}: {exc}",
                })
                continue

            links.append({"uuid": item["uuid"], "wger_exercise_id": exercise.id})
            if was_created:
                created += 1
            else:
                updated += 1

        if not items:
            break

        # Link back in batches so an interrupted run still records its progress.
        if links and not DRY_RUN:
            result = post("/api/exercises/link", {"links": links})
            log(f"  linked {result['updated']} exercises "
                f"({created} created, {updated} updated so far)")
            links = []

        # When importing only pending exercises, each successful page removes those rows
        # from the result set, so the offset must NOT advance or records get skipped.
        if IMPORT_ALL or DRY_RUN:
            offset += PAGE_SIZE
        if IMPORT_LIMIT and (created + updated) >= IMPORT_LIMIT:
            break

    if links and not DRY_RUN:
        result = post("/api/exercises/link", {"links": links})
        log(f"  linked {result['updated']} exercises")

    log("")
    log(f"created: {created}")
    log(f"updated: {updated}")
    log(f"skipped: {skipped + len(all_skipped)}")

    if resolver.unknown_muscles:
        log("")
        log("muscle names not found in wger (mapped to nothing, exercise still imported):")
        for name in sorted(resolver.unknown_muscles):
            log(f"  - {name}")

    if all_skipped:
        log("")
        log(f"skipped detail (first 20 of {len(all_skipped)}):")
        for entry in all_skipped[:20]:
            log(f"  - {entry.get('name')}: {entry.get('reason')}")

    return created, updated


def main():
    log(f"agent: {AGENT_URL}")
    if DRY_RUN:
        log("DRY RUN — no changes will be written")

    before = Exercise.objects.count()
    log(f"wger currently has {before} exercises")

    ensure_equipment()
    import_exercises()

    if not DRY_RUN:
        after = Exercise.objects.count()
        log("")
        log(f"wger now has {after} exercises (+{after - before})")
        log("")
        log("Next: refresh the cached exercise API so the apps see the new exercises:")
        log("  docker compose exec web python3 manage.py warmup-exercise-api-cache --force")
        log("")
        log("Do NOT run sync-exercises with a delete flag: these exercises carry "
            "project-local UUIDs that are absent from upstream's deletion log.")


main()
