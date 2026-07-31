"""Tests for the routine writer and the flat -> nested plan normalizer.

The writer had no test before this: every other suite stubs `create_routine` wholesale,
so the code that turns a plan into ~100 wger requests was the least-verified part of the
project despite being the part that mutates a real training log.

Faking is at the `requests.Session` level rather than at `_post`, so URL construction,
payload shape, error handling and rollback all run for real.

What this file does NOT prove: that wger accepts these payloads. The config semantics —
base values at iteration 1 with operation "r", a progression rule at a later iteration
with repeat=True — are read from wger's schema, which documents the fields but not their
interaction. Only a live server settles that. These tests pin down what we send, so when
a live run disagrees the diff is obvious.

Run: PYTHONPATH=agent:coaching python agent/test_wger_writer.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agent"))
sys.path.insert(0, str(REPO / "coaching"))

import wger_client  # noqa: E402
from routine_schema import normalize_plan  # noqa: E402
from wger_client import WgerClient, WgerError  # noqa: E402

failures: list[str] = []


def check(label, cond, extra=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        failures.append(label)
        print(f"  FAIL  {label} {extra}")


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class FakeSession:
    """Records every call and hands back incrementing ids per endpoint."""

    def __init__(self, fail_on=None, fail_at_call=None):
        self.headers = {}
        self.posts: list[tuple[str, dict]] = []
        self.deletes: list[str] = []
        self.fail_on = fail_on          # endpoint name that should 400
        self.fail_at_call = fail_at_call  # nth post that should 400
        self._ids: dict[str, int] = {}

    def post(self, url, json=None, timeout=None):
        endpoint = url.rstrip("/").rsplit("/", 1)[-1]
        self.posts.append((endpoint, json))
        if self.fail_on == endpoint or self.fail_at_call == len(self.posts):
            return FakeResponse(400, {"detail": f"rejected {endpoint}"})
        self._ids[endpoint] = self._ids.get(endpoint, 100) + 1
        return FakeResponse(201, {"id": self._ids[endpoint]})

    def delete(self, url, timeout=None):
        self.deletes.append(url)
        return FakeResponse(204, {})

    def get(self, url, params=None, timeout=None):
        return FakeResponse(200, {"results": []})


def client(**kwargs) -> tuple[WgerClient, FakeSession]:
    c = WgerClient(base_url="http://web:8000", token="t" * 40)
    session = FakeSession(**kwargs)
    c.session = session
    return c, session


def endpoints(session) -> list[str]:
    return [name for name, _ in session.posts]


def payloads(session, endpoint) -> list[dict]:
    return [body for name, body in session.posts if name == endpoint]


# ---------------------------------------------------------------------------
# The flat wire shape the model actually produces
# ---------------------------------------------------------------------------

FLAT_PLAN = {
    "name": "PPL Return 6d",
    "description": "Six-day push/pull/legs, easing back after a break.",
    "weeks": 6,
    "progression_model": "double_progression",
    "progression_detail": "Add 2.5 kg once you hit the top of the rep range on every set.",
    "rationale": "Six days suits the schedule; volume starts below MAV after a layoff.",
    "days": [
        {"order": 1, "name": "Push A", "is_rest": False, "type": "custom"},
        {"order": 2, "name": "Pull A", "is_rest": False},
        {"order": 3, "name": "Rest", "is_rest": True},
    ],
    "entries": [
        # Day 1, slot 1: a single exercise with a progression rule.
        {"day": 1, "slot": 1, "exercise_id": 3, "exercise_name": "KB Floor Press",
         "sets": 4, "reps": 5, "rir": 2, "rest_seconds": 180, "weight_kg": 32.5,
         "progress_operation": "+", "progress_step": "abs", "progress_value": 2.5,
         "progress_every_iterations": 2},
        # Day 1, slot 2: two entries sharing a slot == a superset.
        {"day": 1, "slot": 2, "exercise_id": 8, "sets": 3, "reps": 10,
         "slot_comment": "superset, no rest between"},
        {"day": 1, "slot": 2, "exercise_id": 5, "sets": 3, "reps": 10},
        # A warmup entry, plus day 2.
        {"day": 2, "slot": 1, "exercise_id": 4, "sets": 2, "reps": 12, "type": "warmup"},
        {"day": 2, "slot": 2, "exercise_id": 2, "sets": 4, "reps": 6, "rest_seconds": 150},
    ],
}

ID_MAP = {2: 4002, 3: 4003, 4: 4004, 5: 4005, 8: 4008}


print("\nnormalizing the wire shape")

plan = normalize_plan(FLAT_PLAN)
check("three days survive", len(plan["days"]) == 3)
check("days keep their order", [d["order"] for d in plan["days"]] == [1, 2, 3])
day1 = plan["days"][0]
check("day 1 has two slots", len(day1["slots"]) == 2, day1)
check("slot 2 is a superset of two entries",
      len(day1["slots"][1]["entries"]) == 2, day1["slots"][1])
check("the slot comment landed on the slot",
      day1["slots"][1]["comment"] == "superset, no rest between")
check("slot/day keys stripped from entries",
      not ({"day", "slot", "slot_comment"} & set(day1["slots"][0]["entries"][0])))
check("progression rebuilt as a nested rule",
      day1["slots"][0]["entries"][0]["progression"] ==
      {"operation": "+", "step": "abs", "value": 2.5, "every_iterations": 2})
check("rest day has no slots", plan["days"][2]["slots"] == [])
check("block progression exposed both ways",
      plan["progression"]["model"] == "double_progression"
      and plan["progression_model"] == "double_progression")


print("\nwriting it to wger")

c, session = client()
result = c.create_routine(plan, ID_MAP)

check("returns the routine id", result["routine_id"] == 101, result)
check("returns a viewable url",
      result["url"] == "http://web:8000/en/routine/101/view/", result)
check("one routine posted", endpoints(session).count("routine") == 1)
check("three days posted", endpoints(session).count("day") == 3)
check("four slots posted — not five",
      endpoints(session).count("slot") == 4, endpoints(session))
check("five slot entries posted", endpoints(session).count("slot-entry") == 5)
check("nothing deleted on success", session.deletes == [])

check("posts go to the v2 api",
      all(u.startswith("http://web:8000/api/v2/") for u in
          [f"http://web:8000/api/v2/{e}/" for e in endpoints(session)]))

routine_body = payloads(session, "routine")[0]
check("routine name sent verbatim", routine_body["name"] == "PPL Return 6d")
check("start and end dates set",
      routine_body["start"] < routine_body["end"], routine_body)

day_bodies = payloads(session, "day")
check("rest day flagged", [d["is_rest"] for d in day_bodies] == [False, False, True])
check("day type defaults to custom when unset",
      day_bodies[1]["type"] == "custom", day_bodies[1])

slot_bodies = payloads(session, "slot")
check("superset slot carries its comment",
      slot_bodies[1]["comment"] == "superset, no rest between", slot_bodies[1])

entry_bodies = payloads(session, "slot-entry")
check("sidecar ids translated to wger ids",
      [e["exercise"] for e in entry_bodies] == [4003, 4008, 4005, 4004, 4002],
      [e["exercise"] for e in entry_bodies])
check("superset entries ordered 1 and 2 within their slot",
      [entry_bodies[1]["order"], entry_bodies[2]["order"]] == [1, 2], entry_bodies[1:3])
check("warmup type preserved", entry_bodies[3]["type"] == "warmup", entry_bodies[3])
check("type defaults to normal", entry_bodies[0]["type"] == "normal")


print("\nprescription configs")

sets_cfg = payloads(session, "sets-config")
reps_cfg = payloads(session, "repetitions-config")
rest_cfg = payloads(session, "rest-config")
rir_cfg = payloads(session, "rir-config")
weight_cfg = payloads(session, "weight-config")

check("one sets config per entry", len(sets_cfg) == 5, len(sets_cfg))
check("one reps config per entry", len(reps_cfg) == 5, len(reps_cfg))
check("rest config only where rest was prescribed", len(rest_cfg) == 2, len(rest_cfg))
check("rir config only where rir was prescribed", len(rir_cfg) == 1, len(rir_cfg))
check("base configs sit at iteration 1 with operation r",
      all(c_["iteration"] == 1 and c_["operation"] == "r" and c_["repeat"] is False
          for c_ in sets_cfg + reps_cfg), sets_cfg[0])
check("values are sent as strings", all(isinstance(c_["value"], str) for c_ in sets_cfg))

# One base weight (replace at iteration 1) plus one progression rule (repeat, later).
base_weight = [w for w in weight_cfg if w["repeat"] is False]
progression = [w for w in weight_cfg if w["repeat"] is True]
check("base weight written once", len(base_weight) == 1, weight_cfg)
check("one progression rule written", len(progression) == 1, weight_cfg)
check("progression repeats from iteration 1 + every_iterations",
      progression[0]["iteration"] == 3, progression[0])
check("progression carries the model's operation and step",
      progression[0]["operation"] == "+" and progression[0]["step"] == "abs")
check("progression value sent as a string", progression[0]["value"] == "2.5")


print("\nrefusing to write a plan wger would reject")

c, session = client()
too_long = dict(plan, name="A routine name that is far beyond twenty-five characters")
try:
    c.create_routine(too_long, ID_MAP)
    check("an over-long name is refused", False, "no exception raised")
except WgerError as exc:
    check("an over-long name is refused", "limit is 25" in str(exc), str(exc)[:120])
check("nothing was posted before the limit check", session.posts == [])

c, session = client()
try:
    c.create_routine(plan, {})       # nothing imported
    check("an unimported exercise is refused", False, "no exception raised")
except WgerError as exc:
    check("an unimported exercise is refused", "never imported" in str(exc), str(exc)[:160])


print("\nrollback")

c, session = client(fail_on="rir-config")
try:
    c.create_routine(plan, ID_MAP)
    check("a mid-write failure raises", False, "no exception raised")
except WgerError as exc:
    check("a mid-write failure raises", True)
    check("the message says it rolled back", "rolled back" in str(exc), str(exc)[:200])
check("the routine was deleted",
      session.deletes == ["http://web:8000/api/v2/routine/101/"], session.deletes)


class RefusingSession(FakeSession):
    def delete(self, url, timeout=None):
        self.deletes.append(url)
        return FakeResponse(500, {"detail": "nope"})


c, _ = client()
refusing = RefusingSession(fail_on="slot-entry")
c.session = refusing
try:
    c.create_routine(plan, ID_MAP)
    check("a failed rollback raises", False, "no exception raised")
except WgerError as exc:
    # Silence here would be the dangerous outcome: debris left in a real account with
    # nothing saying so.
    check("a failed rollback names the orphaned routine",
          "should be deleted manually" in str(exc), str(exc)[:240])


print("\nan already-nested plan still writes (stored payloads predate the flat shape)")

c, session = client()
nested_only = {k: v for k, v in plan.items() if k != "progression_model"}
c.create_routine(nested_only, ID_MAP)
check("nested input needs no normalization", endpoints(session).count("slot") == 4)


print()
if failures:
    print(f"{len(failures)} FAILED: " + "; ".join(failures))
    sys.exit(1)
print("all writer tests passed")
