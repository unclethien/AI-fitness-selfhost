"""Tests for the wger import path.

Two halves, both exercised without Postgres or Django:

  1. The agent's import API, against a fake sidecar database.
  2. The import script's logic, against fake Django models — proving idempotency, the
     paging behaviour, per-exercise transaction isolation, and that unmapped taxonomy
     degrades rather than aborting a 3,000-row run.

Run: python wger_import/test_import.py
"""

from __future__ import annotations

import json
import sys
import uuid as uuidlib
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agent"))

failures: list[str] = []


def check(label, cond, extra=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        failures.append(label)
        print(f"  FAIL  {label} {extra}")


# ===========================================================================
# Part 1 — the agent's import API
# ===========================================================================

import psycopg  # noqa: E402

ROWS = [
    {
        "id": 1,
        "uuid": uuidlib.uuid5(uuidlib.NAMESPACE_URL, "kb-swing"),
        "name": "Kettlebell Swing",
        "description": "**Kettlebell Swing** is an intermediate compound exercise. " * 2,
        "wger_category": 9,          # Legs
        "wger_muscles": [8],         # Gluteus maximus
        "wger_muscles_secondary": [11],  # Biceps femoris
        "wger_equipment": [10],      # Kettlebell
        "primary_equipment": "Kettlebell",
        "secondary_equipment": "None",
        "video_demo_url": "https://youtu.be/x",
        "qc_flags": [],
    },
    {
        "id": 2,
        "uuid": uuidlib.uuid5(uuidlib.NAMESPACE_URL, "clubbell-swipe"),
        "name": "Clubbell Swipe",
        # Long enough to trigger the description trim, with a paragraph break to cut on.
        "description": ("A" * 700) + "\n\n" + ("B" * 400),
        "wger_category": 13,         # Shoulders
        "wger_muscles": [2],
        "wger_muscles_secondary": [],
        "wger_equipment": [],        # Clubbell has no wger equivalent
        "primary_equipment": "Clubbell",
        "secondary_equipment": "None",
        "video_demo_url": None,
        "qc_flags": ["unsorted_force_type"],
    },
    {
        "id": 3,
        "uuid": uuidlib.uuid5(uuidlib.NAMESPACE_URL, "broken"),
        "name": "Unmapped Category Exercise",
        "description": "x" * 100,
        "wger_category": None,       # cannot be created in wger
        "wger_muscles": [],
        "wger_muscles_secondary": [],
        "wger_equipment": [],
        "primary_equipment": "Bodyweight",
        "secondary_equipment": "None",
        "video_demo_url": None,
        "qc_flags": ["unmapped_category"],
    },
]

STATE = {"links": []}


class FakeCursor:
    def __init__(self, row_factory=None):
        self._rows = []

    def execute(self, sql, params=None):
        low = " ".join(sql.split()).lower()
        self._rows = []
        if low.startswith("select count(*)"):
            self._rows = [{"n": len(ROWS)}]
        elif "from exercises" in low and low.startswith("select id, uuid"):
            limit, offset = params
            self._rows = ROWS[offset:offset + limit]
        elif low.startswith("select source"):
            self._rows = [{"source": "ffed-2.9", "total": 3, "linked": 0,
                           "missing_description": 0}]

    def executemany(self, sql, seq):
        STATE["links"].extend(seq)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows

    def __enter__(self): return self
    def __exit__(self, *a): return False


class FakeConn:
    def cursor(self, row_factory=None): return FakeCursor(row_factory)
    def commit(self): pass
    def close(self): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False


psycopg.connect = lambda *a, **k: FakeConn()

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import import_api  # noqa: E402

app = FastAPI()
app.include_router(import_api.build_router(lambda: "postgresql://fake"))
client = TestClient(app)

print("=== import API")

r = client.get("/api/equipment/to-create")
check("equipment/to-create returns the 26 custom types",
      r.status_code == 200 and len(r.json()["names"]) == 26,
      str(r.json().get("names", []))[:120])
check("includes Clubbell", "Clubbell" in r.json()["names"])

r = client.get("/api/exercises/for-import")
check("for-import is 200", r.status_code == 200, r.text[:300])
payload = r.json()
check("returns only creatable exercises", payload["returned"] == 2,
      f"returned {payload['returned']}")
check("unmapped category is skipped, not silently dropped",
      any(s["reason"] == "no mapped wger category" for s in payload["skipped"]),
      str(payload["skipped"]))

items = {i["name"]: i for i in payload["items"]}
check("category resolved to a wger NAME", items["Kettlebell Swing"]["category"] == "Legs",
      items["Kettlebell Swing"]["category"])
check("muscles resolved to wger names",
      items["Kettlebell Swing"]["muscles"] == ["Gluteus maximus"],
      str(items["Kettlebell Swing"]["muscles"]))
check("secondary muscles resolved",
      items["Kettlebell Swing"]["muscles_secondary"] == ["Biceps femoris"],
      str(items["Kettlebell Swing"]["muscles_secondary"]))
check("mapped equipment uses the wger name",
      items["Kettlebell Swing"]["equipment"] == ["Kettlebell"],
      str(items["Kettlebell Swing"]["equipment"]))
check("unmapped equipment passed through by name for creation",
      items["Clubbell Swipe"]["equipment"] == ["Clubbell"],
      str(items["Clubbell Swipe"]["equipment"]))
check("long description trimmed below the cap",
      len(items["Clubbell Swipe"]["description_source"]) <= import_api.MAX_DESCRIPTION_SOURCE,
      str(len(items["Clubbell Swipe"]["description_source"])))
check("description trimmed on a paragraph boundary",
      not items["Clubbell Swipe"]["description_source"].endswith("A" * 5) or
      "\n\n" not in items["Clubbell Swipe"]["description_source"])
check("license attribution present", "Functional Fitness" in payload["license_author"])
check("names truncated to wger's 200-char limit",
      all(len(i["name"]) <= 200 for i in payload["items"]))

r = client.post("/api/exercises/link", json={"links": [
    {"uuid": str(ROWS[0]["uuid"]), "wger_exercise_id": 5001},
    {"uuid": str(ROWS[1]["uuid"]), "wger_exercise_id": 5002},
]})
check("link is 200", r.status_code == 200, r.text[:200])
check("link updates both", r.json()["updated"] == 2, r.text)
check("link writes (wger_id, uuid) pairs",
      STATE["links"] and STATE["links"][0] == (5001, str(ROWS[0]["uuid"])),
      str(STATE["links"][:2]))

r = client.post("/api/exercises/link", json={"links": []})
check("empty link payload is a no-op, not an error",
      r.status_code == 200 and r.json()["updated"] == 0)

r = client.post("/api/exercises/link", json={"links": [{"uuid": None}]})
check("malformed link payload is rejected", r.status_code == 400, str(r.status_code))

r = client.get("/api/import/status")
check("import/status is 200", r.status_code == 200, r.text[:200])


# ===========================================================================
# Part 2 — the import script against fake Django models
# ===========================================================================

print("\n=== import script logic")

DB: dict[str, dict] = {"exercises": {}, "translations": {}, "equipment": {}}
NEXT_ID = {"exercise": 100}


class FakeM2M:
    def __init__(self):
        self.items = []
    def set(self, values):
        self.items = list(values)


class FakeExercise:
    def __init__(self, uuid, category, license_author):
        self.uuid = uuid
        self.category = category
        self.license_author = license_author
        NEXT_ID["exercise"] += 1
        self.id = NEXT_ID["exercise"]
        self.muscles = FakeM2M()
        self.muscles_secondary = FakeM2M()
        self.equipment = FakeM2M()


class ExerciseManager:
    def update_or_create(self, uuid=None, defaults=None):
        defaults = defaults or {}
        if uuid in DB["exercises"]:
            existing = DB["exercises"][uuid]
            existing.category = defaults.get("category", existing.category)
            return existing, False
        obj = FakeExercise(uuid, defaults.get("category"), defaults.get("license_author"))
        DB["exercises"][uuid] = obj
        return obj, True

    def filter(self, uuid=None):
        return SimpleNamespace(exists=lambda: uuid in DB["exercises"])

    def count(self):
        return len(DB["exercises"])


class TranslationManager:
    def update_or_create(self, exercise=None, language=None, defaults=None):
        key = (exercise.id, language)
        created = key not in DB["translations"]
        DB["translations"][key] = dict(defaults or {})
        return SimpleNamespace(**(defaults or {})), created


class EquipmentManager:
    def values_list(self, field, flat=False):
        return list(DB["equipment"])
    def all(self):
        return [SimpleNamespace(name=n) for n in DB["equipment"]]
    def bulk_create(self, objs, ignore_conflicts=False):
        for o in objs:
            DB["equipment"][o.name] = o
    def get_or_create(self, name=None):
        if name not in DB["equipment"]:
            DB["equipment"][name] = SimpleNamespace(name=name)
            return DB["equipment"][name], True
        return DB["equipment"][name], False


WGER_CATEGORIES = ["Abs", "Arms", "Back", "Calves", "Cardio", "Chest", "Legs", "Shoulders"]
WGER_MUSCLES = ["Gluteus maximus", "Biceps femoris", "Anterior deltoid",
                "Quadriceps femoris", "Pectoralis major"]


def install_fake_django(monkey_fail_on=None):
    """Install fake wger/Django modules so the script can be imported and run."""
    import types

    DB["exercises"].clear()
    DB["translations"].clear()
    DB["equipment"].clear()
    for name in ["Barbell", "Dumbbell", "Kettlebell", "Bench"]:
        DB["equipment"][name] = SimpleNamespace(name=name)

    class FakeEquipment:
        objects = EquipmentManager()
        def __init__(self, name=None):
            self.name = name

    class FakeExerciseModel:
        objects = ExerciseManager()

    class FakeTranslation:
        objects = TranslationManager()

    class FakeCategory:
        objects = SimpleNamespace(
            all=lambda: [SimpleNamespace(name=n) for n in WGER_CATEGORIES]
        )

    class FakeMuscle:
        objects = SimpleNamespace(
            all=lambda: [SimpleNamespace(name=n) for n in WGER_MUSCLES]
        )

    class FakeLanguage:
        objects = SimpleNamespace(get=lambda short_name=None: f"lang:{short_name}")

    # django.db.transaction.atomic — a real context manager so a raising body is caught
    # by the script's own except, exactly as Django would behave.
    class Atomic:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    django = types.ModuleType("django")
    django_db = types.ModuleType("django.db")
    django_db.transaction = SimpleNamespace(atomic=lambda: Atomic())
    django.db = django_db
    sys.modules["django"] = django
    sys.modules["django.db"] = django_db

    wger = types.ModuleType("wger")
    core = types.ModuleType("wger.core")
    core_models = types.ModuleType("wger.core.models")
    core_models.Language = FakeLanguage
    exercises = types.ModuleType("wger.exercises")
    ex_models = types.ModuleType("wger.exercises.models")
    ex_models.Equipment = FakeEquipment
    ex_models.Exercise = FakeExerciseModel
    ex_models.ExerciseCategory = FakeCategory
    ex_models.Muscle = FakeMuscle
    ex_models.Translation = FakeTranslation
    utils = types.ModuleType("wger.utils")
    constants = types.ModuleType("wger.utils.constants")
    constants.ENGLISH_SHORT_NAME = "en"
    for name, module in [
        ("wger", wger), ("wger.core", core), ("wger.core.models", core_models),
        ("wger.exercises", exercises), ("wger.exercises.models", ex_models),
        ("wger.utils", utils), ("wger.utils.constants", constants),
    ]:
        sys.modules[name] = module


def run_script(env: dict, api_items, api_skipped=None):
    """Execute import_exercises.py with fake Django and a stubbed agent API."""
    import io
    import contextlib
    import urllib.request

    install_fake_django()

    served = {"pages": 0, "posts": []}

    class FakeResponse(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    # Mirrors the real endpoint: once an exercise is linked it stops being "pending",
    # so it drops out of subsequent only_pending pages. Without modelling that, the
    # script's offset-stays-at-zero paging would legitimately never terminate.
    linked: set[str] = set()

    def fake_urlopen(request, timeout=None):
        url = request if isinstance(request, str) else request.full_url
        if "/api/equipment/to-create" in url:
            body = {"names": ["Clubbell", "Macebell", "Sliders"]}
        elif "/api/exercises/for-import" in url:
            offset = 0
            if "offset=" in url:
                offset = int(url.split("offset=")[1].split("&")[0])
            only_pending = "only_pending=true" in url
            page = api_items[offset // 250] if offset // 250 < len(api_items) else []
            if only_pending:
                page = [i for i in page if i["uuid"] not in linked]
            served["pages"] += 1
            body = {
                "total_matching": sum(len(p) for p in api_items),
                "returned": len(page),
                "offset": offset,
                "skipped": api_skipped or [],
                "license_author": "Functional Fitness Exercise Database v2.9",
                "items": page,
            }
        elif "/api/exercises/link" in url:
            payload = json.loads(request.data.decode())
            served["posts"].append(payload)
            linked.update(link["uuid"] for link in payload["links"])
            body = {"updated": len(payload["links"])}
        else:
            raise AssertionError(f"unexpected URL {url}")
        return FakeResponse(json.dumps(body).encode())

    original = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    old_env = {}
    for k, v in env.items():
        old_env[k] = __import__("os").environ.get(k)
        __import__("os").environ[k] = v

    output = io.StringIO()
    try:
        source = (REPO / "wger_import" / "import_exercises.py").read_text()
        namespace: dict = {"__name__": "__imported__"}
        with contextlib.redirect_stdout(output):
            try:
                exec(compile(source, "import_exercises.py", "exec"), namespace)
            except SystemExit as exc:
                # The script exits deliberately on unrecoverable input (unknown
                # category, unreachable agent). Capture it rather than letting it kill
                # the test run — the exit itself is the behaviour under test.
                served["exit_code"] = exc.code
    finally:
        urllib.request.urlopen = original
        for k, v in old_env.items():
            if v is None:
                __import__("os").environ.pop(k, None)
            else:
                __import__("os").environ[k] = v

    return output.getvalue(), served


def item(uuid_seed, name, category="Legs", muscles=None, equipment=None):
    return {
        "sidecar_id": abs(hash(uuid_seed)) % 10000,
        "uuid": str(uuidlib.uuid5(uuidlib.NAMESPACE_URL, uuid_seed)),
        "name": name,
        "description_source": f"**{name}** is a compound exercise for testing purposes.",
        "category": category,
        "muscles": muscles if muscles is not None else ["Gluteus maximus"],
        "muscles_secondary": [],
        "equipment": equipment if equipment is not None else ["Kettlebell"],
    }


# --- happy path -------------------------------------------------------------
out, served = run_script({}, [[item("a", "Swing"), item("b", "Snatch")], []])
check("creates both exercises", "created: 2" in out, out[-400:])
check("creates missing equipment", "equipment: created 3" in out, out[:300])
check("links back to the sidecar", served["posts"] and
      len(served["posts"][0]["links"]) == 2, str(served["posts"]))
check("reports the new exercise count", "wger now has 2 exercises (+2)" in out, out[-300:])
check("warns against destructive sync", "Do NOT run sync-exercises" in out)
check("suggests the cache warmup", "warmup-exercise-api-cache" in out)

# --- idempotency ------------------------------------------------------------
# Same uuids twice in one run: second occurrence must update, not duplicate.
out, served = run_script({"IMPORT_ALL": "1"},
                         [[item("a", "Swing"), item("a", "Swing Renamed")], []])
check("same uuid updates instead of duplicating",
      "created: 1" in out and "updated: 1" in out, out[-300:])

# --- taxonomy degradation ---------------------------------------------------
out, served = run_script({}, [[item("c", "Odd", muscles=["Nonexistent Muscle"])], []])
check("unknown muscle does not abort the run", "created: 1" in out, out[-300:])
check("unknown muscle is reported", "Nonexistent Muscle" in out, out[-500:])

# --- per-exercise isolation -------------------------------------------------
# A bad category is fatal in the resolver, so use a name wger does not have and check
# the script exits loudly rather than silently importing nothing.
out, served = run_script({}, [[item("d", "Bad", category="Nonsense Category")], []])
check("unknown category fails loudly",
      "has no exercise category" in out or "ERROR" in out, out[-400:])

# --- dry run ----------------------------------------------------------------
out, served = run_script({"DRY_RUN": "1"}, [[item("e", "X"), item("f", "Y")], []])
check("dry run reports without writing", "DRY RUN" in out and "created: 2" in out,
      out[-300:])
check("dry run does not link", not served["posts"], str(served["posts"]))
check("dry run does not create equipment", "would create 3" in out, out[:300])

# --- limit ------------------------------------------------------------------
out, served = run_script({"IMPORT_LIMIT": "1"},
                         [[item("g", "One"), item("h", "Two")], []])
check("IMPORT_LIMIT stops early", "created: 1" in out, out[-300:])
check("IMPORT_LIMIT is announced", "reached IMPORT_LIMIT=1" in out)

# --- unreachable agent ------------------------------------------------------
import urllib.error  # noqa: E402
import urllib.request as urlreq  # noqa: E402

install_fake_django()
original = urlreq.urlopen


def refuse(*a, **k):
    raise urllib.error.URLError("connection refused")


urlreq.urlopen = refuse
try:
    import io
    import contextlib
    output = io.StringIO()
    try:
        with contextlib.redirect_stdout(output):
            exec(compile((REPO / "wger_import" / "import_exercises.py").read_text(),
                         "import_exercises.py", "exec"), {"__name__": "__imported__"})
        check("unreachable agent exits", False, "no SystemExit")
    except SystemExit as exc:
        text = output.getvalue()
        check("unreachable agent exits non-zero", exc.code == 1, str(exc.code))
        check("unreachable agent explains why",
              "cannot reach the agent service" in text, text[-300:])
finally:
    urlreq.urlopen = original


print(f"\n{'ALL PASSED' if not failures else str(len(failures)) + ' FAILURES: ' + str(failures)}")
sys.exit(1 if failures else 0)
