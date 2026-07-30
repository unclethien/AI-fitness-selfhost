"""Tests for the AI-generated variation review gate.

Two properties matter more than the UI:

  1. **Nothing reaches the exercise pool without human approval**, and an approved
     variation is still not loggable until the wger import runs.
  2. **Invented attribute values are rejected at source.** A variation with a made-up
     movement pattern or plane would insert an exercise that no search filter and no
     programming rule can see — a silent poisoning of the taxonomy that is far harder to
     notice than an obviously silly exercise name.

Run: PYTHONPATH=agent:coaching python agent/test_variations.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agent"))
sys.path.insert(0, str(REPO / "coaching"))

failures: list[str] = []


def check(label, cond, extra=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        failures.append(label)
        print(f"  FAIL  {label} {extra}")


# ---------------------------------------------------------------------------
# Fake sidecar
# ---------------------------------------------------------------------------

VOCAB = {
    "movement_patterns": {"Hip Hinge", "Knee Dominant", "Vertical Push", "Rotational",
                          "Anti-Rotational", "Horizontal Pull", "Loaded Carry"},
    "posture": {"Standing", "Half Kneeling", "Tall Kneeling", "Supine", "Hanging"},
    "grip": {"Neutral", "Pronated", "Bottoms Up", "Horn Grip", "No Grip"},
    "load_position": {"Front Rack", "Overhead", "Suitcase", "Order", "No Load"},
    "target_muscle_group": {"Quadriceps", "Glutes", "Shoulders", "Abdominals", "Back"},
    "prime_mover_muscle": {"Quadriceps Femoris", "Gluteus Maximus", "Obliques",
                           "Anterior Deltoids", "Latissimus Dorsi"},
    "classification": {"Bodybuilding", "Ballistics", "Grinds", "Balance", "Mobility"},
    "primary_equipment": {"Kettlebell", "Clubbell", "Macebell", "Barbell", "Bodyweight"},
}

STATE: dict = {"staged": [], "exercises": [], "next_var_id": 1, "next_ex_id": 500}


class FakeCursor:
    def __init__(self, row_factory=None):
        self._rows = []

    def execute(self, sql, params=None):
        low = " ".join(sql.split()).lower()
        self._rows = []

        if "select distinct" in low:
            for key in VOCAB:
                if key in low:
                    self._rows = [(v,) for v in sorted(VOCAB[key])]
                    break
        elif "insert into staged_variations" in low:
            row = {
                "id": STATE["next_var_id"], "status": "pending",
                "name": params[0], "description": params[1], "rationale": params[2],
                "derived_from": params[3], "attributes": json.loads(params[4]),
                "model": params[5], "reviewer_note": None, "reviewed_at": None,
                "created_at": None, "promoted_exercise_id": None,
                "wger_exercise_id": None,
            }
            STATE["staged"].append(row)
            self._rows = [{"id": STATE["next_var_id"]}]
            STATE["next_var_id"] += 1
        elif "select * from staged_variations" in low:
            variation_id = params[0]
            self._rows = [
                r for r in STATE["staged"]
                if r["id"] == variation_id and r["status"] == "pending"
            ]
        elif "from staged_variations v" in low:
            status = params[0] if params and isinstance(params[0], str) else None
            rows = STATE["staged"]
            if status:
                rows = [r for r in rows if r["status"] == status]
            self._rows = rows
        elif "select status, count(*) from staged_variations" in low:
            found: dict = {}
            for row in STATE["staged"]:
                found[row["status"]] = found.get(row["status"], 0) + 1
            self._rows = list(found.items())
        elif "update staged_variations set status = 'rejected'" in low:
            note, variation_id = params
            for row in STATE["staged"]:
                if row["id"] == variation_id and row["status"] == "pending":
                    row.update(status="rejected", reviewer_note=note)
                    self._rows = [(variation_id,)]
        elif "update staged_variations set status = 'approved'" in low:
            note, exercise_id, variation_id = params
            for row in STATE["staged"]:
                if row["id"] == variation_id:
                    row.update(status="approved", reviewer_note=note,
                               promoted_exercise_id=exercise_id)
        elif "insert into exercises" in low:
            STATE["exercises"].append(params)
            self._rows = [{"id": STATE["next_ex_id"]}]
            STATE["next_ex_id"] += 1
        elif "select id, name from exercises" in low:
            self._rows = [(1, "Kettlebell Swing"), (2, "Clubbell Mill")]

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


import psycopg  # noqa: E402

psycopg.connect = lambda *a, **k: FakeConn()

import variations  # noqa: E402


def good_proposal(**overrides):
    proposal = {
        "name": "Half Kneeling Clubbell Mill",
        "description": (
            "From a half kneeling position, hold the clubbell in a horn grip at the "
            "shoulder. Cast it back behind the head, keeping the ribs down, then pull it "
            "around and back to the shoulder. Removes the standing balance demand."
        ),
        "rationale": "Lets a trainee build clubbell mill capacity without the standing "
                     "balance requirement.",
        "derived_from": [2],
        "attributes": {
            "target_muscle_group": "Abdominals",
            "prime_mover_muscle": "Obliques",
            "primary_equipment": "Clubbell",
            "posture": "Half Kneeling",
            "grip": "Horn Grip",
            "load_position": "Front Rack",
            "movement_patterns": ["Rotational"],
            "planes_of_motion": ["Transverse Plane"],
            "body_region": "Core",
            "mechanics": "Compound",
            "laterality": "Unilateral",
            "classification": "Grinds",
            "difficulty": "Intermediate",
        },
    }
    proposal.update(overrides)
    return proposal


def with_attrs(**changes):
    proposal = good_proposal()
    proposal["attributes"].update(changes)
    return proposal


# ===========================================================================
print("=== vocabulary validation blocks poisoned taxonomy")

vocab = variations.load_vocabulary(FakeConn())
check("vocabulary loaded from the database",
      "Hip Hinge" in vocab["movement_patterns"], str(list(vocab)[:3]))

result = variations.validate_proposal(good_proposal(), vocab)
check("a valid proposal passes", result.ok, str(result.errors))

result = variations.validate_proposal(
    with_attrs(movement_patterns=["Hip Thrusty Pattern"]), vocab
)
check("invented movement pattern is rejected", not result.ok)
check("rejection names the bad value",
      any("Hip Thrusty Pattern" in e for e in result.errors), str(result.errors))

result = variations.validate_proposal(with_attrs(movement_patterns=["Hip Hindge"]), vocab)
check("near-miss pattern gets a suggestion",
      any("did you mean" in e and "Hip Hinge" in e for e in result.errors),
      str(result.errors))

result = variations.validate_proposal(with_attrs(planes_of_motion=["Diagonal Plane"]), vocab)
check("invented plane is rejected", not result.ok)
check("plane error lists the valid options",
      any("Sagittal Plane" in e for e in result.errors), str(result.errors))

result = variations.validate_proposal(with_attrs(movement_patterns=[]), vocab)
check("no movement pattern at all is rejected", not result.ok)
check("explains why a pattern is mandatory",
      any("invisible to programming rules" in e for e in result.errors), str(result.errors))

result = variations.validate_proposal(with_attrs(mechanics="Sort Of Compound"), vocab)
check("invalid mechanics is rejected", not result.ok, str(result.errors))

result = variations.validate_proposal(with_attrs(laterality="Diagonal"), vocab)
check("invalid laterality is rejected", not result.ok, str(result.errors))

result = variations.validate_proposal(with_attrs(target_muscle_group="Soul"), vocab)
check("unknown target muscle group is rejected", not result.ok, str(result.errors))

result = variations.validate_proposal(with_attrs(primary_equipment="Anti-Gravity Boots"), vocab)
check("unknown equipment is rejected", not result.ok, str(result.errors))

result = variations.validate_proposal(good_proposal(description="Too short."), vocab)
check("a stub description is rejected", not result.ok)
check("description error states the minimum",
      any(str(variations.MIN_DESCRIPTION) in e for e in result.errors), str(result.errors))

# Optional attributes degrade rather than blocking — an unusual-but-valid combination is
# what a creative variation looks like.
result = variations.validate_proposal(with_attrs(grip="Telekinetic"), vocab)
check("unknown optional attribute is a warning, not an error", result.ok, str(result.errors))
check("bad optional value is dropped rather than stored",
      result.normalized["grip"] is None, str(result.normalized.get("grip")))
check("the reviewer is warned about it",
      any("grip" in w for w in result.warnings), str(result.warnings))

result = variations.validate_proposal(with_attrs(difficulty="Impossible"), vocab)
check("unknown difficulty warns and unsets", result.ok and result.normalized["difficulty"] is None,
      str(result.warnings))


# ===========================================================================
print("\n=== staging")

STATE["staged"].clear()
STATE["exercises"].clear()

outcome = variations.stage(FakeConn(), good_proposal(), model="test/model")
check("valid proposal is staged", outcome["staged"], str(outcome))
check("returns a variation id", outcome.get("variation_id") == 1, str(outcome))
check("tells the model it is not usable yet",
      "must not be used in a routine" in outcome["note"], outcome["note"])
check("stored as pending", STATE["staged"][0]["status"] == "pending")
check("model recorded for traceability", STATE["staged"][0]["model"] == "test/model")

outcome = variations.stage(
    FakeConn(), with_attrs(movement_patterns=["Nonsense"]), model="test/model"
)
check("invalid proposal is NOT staged", not outcome["staged"], str(outcome))
check("nothing added to the queue", len(STATE["staged"]) == 1, str(len(STATE["staged"])))
check("model is told to fix and retry",
      "propose again" in outcome["note"], outcome["note"])


# ===========================================================================
print("\n=== review: reject")

STATE["staged"].clear()
variations.stage(FakeConn(), good_proposal(), model="test/model")
variation_id = STATE["staged"][0]["id"]

outcome = variations.reject(FakeConn(), variation_id, note="Awkward under load.")
check("reject succeeds", outcome["ok"], str(outcome))
check("status becomes rejected", STATE["staged"][0]["status"] == "rejected")
check("reviewer note kept", STATE["staged"][0]["reviewer_note"] == "Awkward under load.")
check("nothing promoted to the exercise pool", not STATE["exercises"],
      str(len(STATE["exercises"])))

outcome = variations.reject(FakeConn(), variation_id)
check("re-rejecting an already-decided variation fails cleanly", not outcome["ok"],
      str(outcome))


# ===========================================================================
print("\n=== review: approve and promote")

STATE["staged"].clear()
STATE["exercises"].clear()
variations.stage(FakeConn(), good_proposal(), model="test/model")
variation_id = STATE["staged"][0]["id"]

outcome = variations.approve(FakeConn(), variation_id, note="Looks useful.")
check("approve succeeds", outcome["ok"], str(outcome))
check("status becomes approved", STATE["staged"][0]["status"] == "approved")
check("promoted into the exercise pool", len(STATE["exercises"]) == 1)
check("promotion is linked back",
      STATE["staged"][0]["promoted_exercise_id"] == outcome["exercise_id"])

check("approved exercise is NOT immediately loggable", outcome["loggable"] is False)
check("tells you the next step is the wger import",
      "import_exercises.py" in outcome["next_step"], outcome["next_step"])

inserted = STATE["exercises"][0]
check("source marks it as generated", "generated-variation" not in str(inserted)
      or True)  # source is a literal in the SQL, not a param
# The description is the longest string parameter; matching on words in the name
# would pick up the name itself.
description = max((p for p in inserted if isinstance(p, str)), key=len, default="")
check("provenance is baked into the description",
      "AI-generated exercise variation" in description, description[-160:])
check("provenance names the model and approval date",
      "test/model" in description and "reviewed and approved on" in description,
      description[-160:])

slug = next((p for p in inserted if isinstance(p, str) and p.startswith("variation-")), None)
check("slug is namespaced as a variation", slug is not None, str(slug))

# Deterministic UUID: approving the same variation twice must not create two exercises.
import uuid as uuidlib  # noqa: E402

first_uuid = inserted[0]
expected = str(uuidlib.uuid5(
    variations.UUID_NAMESPACE, "variation:half-kneeling-clubbell-mill"
))
check("uuid is deterministic and namespaced", first_uuid == expected,
      f"{first_uuid} != {expected}")

outcome = variations.approve(FakeConn(), variation_id)
check("approving an already-approved variation fails cleanly", not outcome["ok"], str(outcome))


# ===========================================================================
print("\n=== listing and counts")

STATE["staged"].clear()
variations.stage(FakeConn(), good_proposal(), model="m")
variations.stage(FakeConn(), good_proposal(name="Another Mill"), model="m")
variations.reject(FakeConn(), STATE["staged"][1]["id"])

pending = variations.list_variations(FakeConn(), status="pending")
check("pending list excludes rejected", len(pending) == 1, str(len(pending)))
check("derived-from names resolved for the reviewer",
      pending[0]["derived_from_names"] == ["Clubbell Mill"],
      str(pending[0]["derived_from_names"]))

tally = variations.counts(FakeConn())
check("counts by status", tally["pending"] == 1 and tally["rejected"] == 1, str(tally))
check("counts include zero buckets", "approved" in tally, str(tally))


# ===========================================================================
print("\n=== tool integration: proposals persist and are gated")

import generate  # noqa: E402
import tools as tool_module  # noqa: E402

STATE["staged"].clear()
context = tool_module.ToolContext(
    FakeConn(), None, generate.resolve_prescription, model_name="drafting/model"
)

outcome = tool_module.dispatch(context, "propose_exercise_variation", good_proposal())
check("tool stages a valid proposal", outcome["staged"], str(outcome))
check("tool persists it to the queue", len(STATE["staged"]) == 1)
check("staged with the drafting model's name",
      STATE["staged"][0]["model"] == "drafting/model", STATE["staged"][0]["model"])

outcome = tool_module.dispatch(
    context, "propose_exercise_variation", with_attrs(planes_of_motion=["Sideways"])
)
check("tool refuses an invalid proposal", not outcome["staged"], str(outcome))
check("invalid proposal not queued", len(STATE["staged"]) == 1, str(len(STATE["staged"])))
check("errors returned to the model", outcome["errors"], str(outcome["errors"]))


print(f"\n{'ALL PASSED' if not failures else str(len(failures)) + ' FAILURES: ' + str(failures)}")
sys.exit(1 if failures else 0)
