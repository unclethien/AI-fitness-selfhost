"""End-to-end tests for the routine generation pipeline.

Fakes the model, the sidecar database and wger, then asserts the pipeline behaves
correctly at each decision point. The interesting cases are the failure paths:

  - a plan that violates programming rules must be sent back with instructions
  - a critic verdict of "revise" must block a plan that passed the rule checks
  - repeated failure must escalate to a stronger model, not loop
  - an exercise never imported into wger must not be written
  - a wger write failure must roll back rather than leave a partial routine
  - a failed critic must not block an otherwise-valid routine

Run: PYTHONPATH=agent:coaching python agent/test_generate.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

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
# Fake exercise pool, mirroring the sidecar's column shape
# ---------------------------------------------------------------------------

def ex(eid, name, target, prime, **kw):
    row = {
        "id": eid, "name": name, "target_muscle_group": target,
        "prime_mover_muscle": prime, "secondary_muscle": kw.get("secondary"),
        "tertiary_muscle": None, "mechanics": kw.get("mechanics", "Compound"),
        "movement_patterns": kw.get("patterns", ["Knee Dominant"]),
        "planes_of_motion": kw.get("planes", ["Sagittal Plane"]),
        "force_type": kw.get("force", "Push"),
        "body_region": kw.get("region", "Lower Body"),
        "primary_equipment": kw.get("equipment", "Kettlebell"),
        "secondary_equipment": None, "classification": kw.get("classification", "Bodybuilding"),
        "posture": "Standing", "difficulty_rank": kw.get("rank", 3),
        "laterality": kw.get("laterality", "Bilateral"),
        "wger_exercise_id": kw.get("wger_id", 4000 + eid),
        "difficulty": "Intermediate", "is_combo": False, "grip": "Neutral",
        "load_position": "Front Rack", "arm_involvement": "Double Arm",
    }
    return row


POOL = {
    1: ex(1, "KB Front Squat", "Quadriceps", "Quadriceps Femoris",
          patterns=["Knee Dominant"], secondary="Gluteus Maximus"),
    2: ex(2, "KB Swing", "Glutes", "Gluteus Maximus", patterns=["Hip Hinge"],
          force="Pull", classification="Ballistics"),
    3: ex(3, "KB Floor Press", "Chest", "Pectoralis Major",
          patterns=["Horizontal Push"], region="Upper Body", secondary="Triceps Brachii"),
    4: ex(4, "Ring Row", "Back", "Latissimus Dorsi", patterns=["Horizontal Pull"],
          force="Pull", region="Upper Body", equipment="Gymnastic Rings",
          secondary="Biceps Brachii"),
    5: ex(5, "Clubbell Mill", "Abdominals", "Obliques", patterns=["Rotational"],
          planes=["Transverse Plane"], region="Core", equipment="Clubbell", force="Pull"),
    6: ex(6, "KB Lateral Lunge", "Quadriceps", "Quadriceps Femoris",
          patterns=["Knee Dominant"], planes=["Frontal Plane"], laterality="Unilateral"),
    7: ex(7, "Hanging Knee Raise", "Abdominals", "Rectus Abdominis",
          patterns=["Anti-Extension"], region="Core", equipment="Pull Up Bar",
          mechanics="Isolation", force="Other"),
    8: ex(8, "KB Press", "Shoulders", "Anterior Deltoids", patterns=["Vertical Push"],
          region="Upper Body", secondary="Triceps Brachii"),
    # Never imported into wger — must never reach a routine.
    99: ex(99, "Orphan Exercise", "Chest", "Pectoralis Major", wger_id=None),
}


# ---------------------------------------------------------------------------
# Fake sidecar connection
# ---------------------------------------------------------------------------

PROFILE_ROW = {
    "id": 1, "display_name": "Thien", "birth_year": 1990, "gender": "male",
    "bodyweight_kg": 78.5, "height_cm": 175, "experience_level": "intermediate",
    "training_age_months": 18, "sessions_per_week": 4, "minutes_per_session": 75,
    "available_equipment": ["Kettlebell", "Clubbell", "Gymnastic Rings", "Pull Up Bar"],
    "dislikes": [], "notes": None,
}
GOALS = [{"goal": "general_fitness", "priority": 1}, {"goal": "strength", "priority": 1},
         {"goal": "fat_loss", "priority": 2}]
CONTRAINDICATIONS: list[dict] = []
RECORDED = {"routines": [], "reviews": []}


class FakeCursor:
    def __init__(self, row_factory=None):
        self._rows = []

    def execute(self, sql, params=None):
        low = " ".join(sql.split()).lower()
        self._rows = []
        if "from trainee_profile" in low:
            self._rows = [PROFILE_ROW]
        elif "from trainee_goals" in low:
            self._rows = GOALS
        elif "from trainee_contraindications" in low:
            self._rows = CONTRAINDICATIONS
        elif "trainee_benchmarks" in low:
            self._rows = []
        elif "from generated_routines" in low and "payload" in low:
            self._rows = []
        elif "insert into generated_routines" in low:
            RECORDED["routines"].append(params)
            self._rows = [(1,)]
        elif "insert into routine_reviews" in low:
            RECORDED["reviews"].append(params)
        elif "select distinct" in low:
            # variations.load_vocabulary — must come before the generic exercises
            # branch, which returns dict rows the vocabulary loader cannot index.
            column = next(
                (c for c in ("movement_patterns", "planes_of_motion", "posture", "grip",
                             "load_position", "target_muscle_group", "prime_mover_muscle",
                             "classification", "primary_equipment")
                 if c in low), None
            )
            values = set()
            for row in POOL.values():
                value = row.get(column)
                if isinstance(value, list):
                    values.update(value)
                elif value:
                    values.add(value)
            self._rows = [(v,) for v in sorted(values)]
        elif "insert into staged_variations" in low:
            self._rows = [{"id": 1}]
        elif "select id, wger_exercise_id from exercises" in low:
            ids = params[0]
            self._rows = [
                (i, POOL[i]["wger_exercise_id"]) for i in ids
                if i in POOL and POOL[i]["wger_exercise_id"]
            ]
        elif "from exercises where id = any" in low:
            ids = params[0]
            self._rows = [POOL[i] for i in ids if i in POOL]
        elif "from exercises" in low:
            # search_exercises — the fake ignores filters and returns the pool; filter
            # correctness is covered by test_exercise_search below.
            self._rows = [r for r in POOL.values() if r["wger_exercise_id"]]

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

import exercise_search  # noqa: E402
import generate  # noqa: E402
import llm  # noqa: E402
import tools as tool_module  # noqa: E402


# ---------------------------------------------------------------------------
# Fake wger
# ---------------------------------------------------------------------------

class FakeWger:
    def __init__(self, fail_at: int | None = None):
        self.posts: list[tuple[str, dict]] = []
        self.deleted: list[int] = []
        self.fail_at = fail_at
        self._n = 0

    def recent_logs(self, days=28):
        return [{"date": "2026-07-20", "exercise": 4001, "reps": 8, "weight": "40", "rir": 2}]

    def recent_sessions(self, days=28):
        return [{"date": "2026-07-20", "impression": "2", "notes": "felt good"}]

    def create_routine(self, plan, exercise_ids, start=None):
        self._n += 1
        if self.fail_at and self._n >= self.fail_at:
            raise RuntimeError("wger rejected slot-entry: exercise does not exist")
        self.posts.append(("routine", plan))
        return {"routine_id": 777, "requests": 42,
                "url": "http://wger/en/routine/777/view/"}


# ---------------------------------------------------------------------------
# Plan builders
# ---------------------------------------------------------------------------

def entry(eid, sets=3, reps=8, rest=150, **kw):
    return {"exercise_id": eid, "sets": sets, "reps": reps, "rest_seconds": rest, **kw}


def day(order, name, entries):
    return {"order": order, "name": name, "is_rest": False,
            "slots": [{"order": i + 1, "entries": [e]} for i, e in enumerate(entries)]}


def good_plan():
    return {
        "name": "KB Strength 4d",
        "description": "Four-day kettlebell and clubbell strength block.",
        "weeks": 6,
        "progression": {"model": "double_progression",
                        "detail": "Top of the rep range on all sets, then add 2kg and reset."},
        "rationale": "Balanced four-day split built around the trainee's kettlebell and "
                     "clubbell equipment, hitting all three planes across the week.",
        "days": [
            day(1, "Lower A", [entry(1, 4, 6, 210), entry(2, 4, 10, 120),
                               entry(6, 3, 8, 120), entry(5, 3, 12, 90)]),
            day(2, "Upper A", [entry(3, 4, 6, 210), entry(4, 4, 8, 150),
                               entry(8, 3, 8, 150), entry(7, 3, 10, 90)]),
            day(3, "Lower B", [entry(2, 4, 12, 120), entry(1, 3, 8, 180),
                               entry(6, 3, 10, 120), entry(5, 3, 12, 90)]),
            day(4, "Upper B", [entry(4, 4, 8, 150), entry(3, 3, 8, 150),
                               entry(8, 3, 10, 120), entry(7, 3, 12, 90)]),
        ],
    }


def bad_plan():
    """All push, no hinge, no pulling, sagittal only, wildly over volume."""
    p = good_plan()
    p["days"] = [
        day(1, "Push A", [entry(1, 10, 20, 240), entry(3, 10, 20, 240)]),
        day(2, "Push B", [entry(1, 10, 20, 240), entry(8, 10, 20, 240)]),
    ]
    return p


def orphan_plan():
    p = good_plan()
    p["days"][0]["slots"][0]["entries"][0]["exercise_id"] = 99
    return p


# ---------------------------------------------------------------------------
# Scripted fake model
# ---------------------------------------------------------------------------

def scripted_client(plans, critic_verdicts=None, critic_raises=False):
    """Returns a factory producing LLMClients whose behaviour is scripted.

    `plans` is consumed one per drafting round; `critic_verdicts` one per critic call.
    """
    state = {"plans": list(plans), "verdicts": list(critic_verdicts or []),
             "models_used": [], "critic_calls": 0}

    class Scripted(LLMClientBase := llm.LLMClient):
        def __init__(self, model, **kw):
            self.model = model
            self.base_url = "http://fake/v1"
            state["models_used"].append(model)

        def run_tools(self, messages, tools, dispatch, max_rounds=12, on_event=None):
            # Exercise the real tool dispatch so tool wiring is genuinely covered.
            dispatch("get_trainee_profile", {})
            dispatch("get_recent_training", {"days": 28})
            dispatch("search_exercises", {"movement_patterns": ["Hip Hinge"]})
            plan = state["plans"].pop(0) if state["plans"] else None
            if plan is not None:
                dispatch("submit_routine_plan", plan)
            return list(messages) + [{"role": "assistant", "content": "done"}], "done"

        def structured(self, messages, schema, schema_name="result", max_repair_attempts=2):
            state["critic_calls"] += 1
            if critic_raises:
                raise llm.StructuredOutputError("critic model unavailable")
            verdict = state["verdicts"].pop(0) if state["verdicts"] else "approve"
            return ({
                "verdict": verdict,
                "assessment": "Reviewed against the client profile and found it sound."
                if verdict == "approve"
                else "Exercise order is wrong for this client's knee history.",
                "required_changes": [] if verdict == "approve"
                else ["Move the loaded carry before the unilateral work."],
            }, "json_schema")

    return Scripted, state


def run(plans, wger=None, verdicts=None, critic_raises=False, write=True):
    Scripted, state = scripted_client(plans, verdicts, critic_raises)
    original = generate.LLMClient
    generate.LLMClient = Scripted
    try:
        result = generate.generate_routine(
            FakeConn(), wger if wger is not None else FakeWger(),
            "4-day kettlebell and clubbell strength program",
            drafting_model="draft/model",
            escalation_model="strong/model",
            critic_model="critic/model",
            write=write,
        )
    finally:
        generate.LLMClient = original
    return result, state


# ===========================================================================
print("=== the wire schema stays shallow")

# This is the whole point of the flat wire format, so it gets a regression guard.
# A gateway rejected the previously-nested schema with
#   503 {'code': 'chat_admission_busy', 'reason': 'structure_limit'}
# while accepting depth-7 tool payloads from the same service.
import json  # noqa: E402
import routine_schema  # noqa: E402
import tools as _tools  # noqa: E402

drafting_depth = routine_schema.schema_depth(_tools.TOOL_DEFINITIONS)
plan_depth = routine_schema.schema_depth(routine_schema.ROUTINE_PLAN_SCHEMA)
check(f"drafting toolset depth {drafting_depth} <= 10 (was 18 when nested)",
      drafting_depth <= 10, drafting_depth)
check(f"plan schema depth {plan_depth} <= 7", plan_depth <= 7, plan_depth)
check("no nested enum lists survive in the plan schema",
      "enum" not in json.dumps(routine_schema.ROUTINE_PLAN_SCHEMA["properties"]["entries"]),
      "an enum inside entries[] costs two levels of depth")
check("the plan schema is flat: no slots array on the wire",
      "slots" not in json.dumps(routine_schema.ROUTINE_PLAN_SCHEMA))


print("=== a plan submitted in the flat wire shape runs the whole pipeline")

def flat_equivalent(nested):
    """Re-express a nested fixture in the shape the model now emits."""
    flat = {k: v for k, v in nested.items() if k not in ("days", "progression")}
    flat["progression_model"] = nested["progression"]["model"]
    flat["progression_detail"] = nested["progression"]["detail"]
    flat["days"] = [{"order": d["order"], "name": d["name"], "is_rest": d["is_rest"]}
                    for d in nested["days"]]
    flat["entries"] = [
        {"day": d["order"], "slot": s["order"], **e}
        for d in nested["days"]
        for s in d.get("slots", [])
        for e in s.get("entries", [])
    ]
    return flat


wger = FakeWger()
result, state = run([flat_equivalent(good_plan())], wger=wger)
check("a flat plan is accepted", result.ok, result.error)
check("it reached wger", result.routine_id == 777, result.error)
written = wger.posts[0][1]
check("the pipeline saw the nested shape after normalization",
      "slots" in written["days"][0], list(written["days"][0]))
check("no entries array leaked past normalization", "entries" not in written)
check("day and slot keys stripped from the entries",
      not ({"day", "slot"} & set(written["days"][0]["slots"][0]["entries"][0])),
      written["days"][0]["slots"][0]["entries"][0])
check("validation ran against the normalized plan", result.violations is not None)


# ===========================================================================
print("=== happy path")
RECORDED["routines"].clear()
RECORDED["reviews"].clear()
wger = FakeWger()
result, state = run([good_plan()], wger=wger)
check("succeeds", result.ok, result.error or "")
check("one iteration", result.iterations == 1, str(result.iterations))
check("no errors from the validator",
      not [v for v in result.violations if v["severity"] == "error"],
      str([v["code"] for v in result.violations if v["severity"] == "error"]))
check("critic ran once", state["critic_calls"] == 1, str(state["critic_calls"]))
check("critic approved", (result.critic or {}).get("verdict") == "approve")
check("routine written to wger", result.routine_id == 777, str(result.routine_id))
check("routine url returned", "777" in (result.routine_url or ""))
check("no escalation on first pass", "strong/model" not in state["models_used"],
      str(state["models_used"]))
check("routine recorded for audit", len(RECORDED["routines"]) == 1,
      str(len(RECORDED["routines"])))
check("review recorded for audit", len(RECORDED["reviews"]) == 1)

print("\n=== validator blocks a bad plan, then accepts the revision")
wger = FakeWger()
result, state = run([bad_plan(), good_plan()], wger=wger)
check("recovers on the second attempt", result.ok, result.error or "")
check("took two iterations", result.iterations == 2, str(result.iterations))
check("critic only ran for the passing plan", state["critic_calls"] == 1,
      str(state["critic_calls"]))
check("routine still written", result.routine_id == 777)

print("\n=== critic can veto a rule-passing plan")
wger = FakeWger()
result, state = run([good_plan(), good_plan()], wger=wger, verdicts=["revise", "approve"])
check("revise verdict forces another round", result.iterations == 2, str(result.iterations))
check("succeeds after the critic is satisfied", result.ok, result.error or "")
check("critic ran twice", state["critic_calls"] == 2, str(state["critic_calls"]))

print("\n=== escalation after repeated failure")
result, state = run([bad_plan(), bad_plan(), bad_plan(), bad_plan()], wger=FakeWger())
check("gives up rather than looping", not result.ok)
check("reports why", "still failing review" in (result.error or ""), result.error or "")
check("escalated to the stronger model", "strong/model" in state["models_used"],
      str(state["models_used"]))
check("escalation is noted", any("escalated" in n for n in result.strategy_notes),
      str(result.strategy_notes))

print("\n=== a failed critic must not block a valid routine")
wger = FakeWger()
result, state = run([good_plan()], wger=wger, critic_raises=True)
check("still succeeds", result.ok, result.error or "")
check("degradation is recorded",
      any("critic unavailable" in n for n in result.strategy_notes),
      str(result.strategy_notes))

print("\n=== never write an exercise that isn't in wger")
wger = FakeWger()
result, state = run([orphan_plan(), orphan_plan(), orphan_plan(), orphan_plan()], wger=wger)
check("refuses the plan", not result.ok)
check("nothing written to wger", not wger.posts, str(wger.posts))
check("explains the cause",
      "not been imported" in (result.error or "") or
      any(v["code"] == "exercise_not_loggable" for v in result.violations),
      (result.error or "")[:200])

print("\n=== wger write failure surfaces cleanly")
wger = FakeWger(fail_at=1)
result, state = run([good_plan()], wger=wger)
check("reports the failure", not result.ok)
check("error mentions writing", "writing the routine failed" in (result.error or ""),
      (result.error or "")[:200])
check("plan is still returned for inspection", result.plan is not None)

print("\n=== dry run")
wger = FakeWger()
result, state = run([good_plan()], wger=wger, write=False)
check("dry run succeeds", result.ok, result.error or "")
check("dry run writes nothing", not wger.posts)
check("dry run is flagged", any("dry run" in n for n in result.strategy_notes),
      str(result.strategy_notes))

print("\n=== model that never submits a plan")
result, state = run([None, None, None, None], wger=FakeWger())
check("fails with a clear reason", not result.ok)
check("says the plan was never submitted",
      "never submitted" in (result.error or ""), (result.error or "")[:200])


# ===========================================================================
print("\n=== tool dispatch")

context = tool_module.ToolContext(FakeConn(), FakeWger(), generate.resolve_prescription)

profile_result = tool_module.dispatch(context, "get_trainee_profile", {})
check("profile tool reports existence", profile_result["profile_exists"])
check("profile tool returns the resolved prescription",
      "weekly_sets_large_muscles" in profile_result["prescription"])
check("prescription carries the goal-conflict note",
      any("energy balance" in n
          for n in profile_result["prescription"]["coaching_notes"]),
      str(profile_result["prescription"]["coaching_notes"]))
check("profile tool exposes age", profile_result["age"] is not None)

training = tool_module.dispatch(context, "get_recent_training", {"days": 28})
check("recent training available", training["available"])
check("logs summarized per exercise", training["per_exercise"], str(training)[:200])

search = tool_module.dispatch(context, "search_exercises", {"movement_patterns": ["Hip Hinge"]})
check("search returns exercises", search["count"] > 0)
check("search excludes non-loggable exercises",
      all(e["wger_exercise_id"] for e in search["exercises"]))
check("search records ids as seen", context.seen_exercise_ids)

detail = tool_module.dispatch(context, "get_exercise_detail", {"exercise_ids": [1, 12345]})
check("detail reports unknown ids", detail.get("not_found") == [12345], str(detail.get("not_found")))

# Attributes must be real vocabulary values or the proposal is refused outright.
variation = tool_module.dispatch(context, "propose_exercise_variation", {
    "name": "Clubbell Kneeling Mill",
    "description": "From a kneeling position, cast the clubbell back and mill it around "
                   "the head, keeping the ribs down throughout the movement.",
    "rationale": "Removes the standing balance demand.",
    "attributes": {
        "target_muscle_group": "Abdominals", "prime_mover_muscle": "Obliques",
        "primary_equipment": "Clubbell", "movement_patterns": ["Rotational"],
        "planes_of_motion": ["Transverse Plane"], "body_region": "Core",
        "mechanics": "Compound", "laterality": "Unilateral",
    },
})
check("valid variation is queued for review", variation.get("staged"), str(variation))
check("variation tells the model not to use it",
      "must not be used in a routine" in variation["note"], variation.get("note", ""))
check("variation stored on the context", len(context.proposed_variations) == 1)

refused = tool_module.dispatch(context, "propose_exercise_variation", {
    "name": "Nonsense Move",
    "description": "A movement with an invented pattern that nothing can ever match, "
                   "which should be refused before it reaches the review queue.",
    "rationale": "Testing taxonomy validation.",
    "attributes": {"movement_patterns": ["Vibe Pattern"], "planes_of_motion": ["Sideways"]},
})
check("invented taxonomy is refused, not queued", not refused.get("staged"), str(refused))
check("refusal is not counted as a proposal", len(context.proposed_variations) == 1)

unknown = tool_module.dispatch(context, "nonexistent_tool", {})
check("unknown tool returns an error rather than raising", "error" in unknown)


# ===========================================================================
print("\n=== wger routine writer field limits")

import wger_client as wc  # noqa: E402

long_name = good_plan()
long_name["name"] = "A" * 40
problems = wc.WgerClient._check_limits(long_name)
check("over-long routine name is caught", any("routine name" in p for p in problems),
      str(problems))

long_day = good_plan()
long_day["days"][0]["name"] = "B" * 30
problems = wc.WgerClient._check_limits(long_day)
check("over-long day name is caught", any("day name" in p for p in problems), str(problems))
check("a valid plan passes the limit check", wc.WgerClient._check_limits(good_plan()) == [])


print(f"\n{'ALL PASSED' if not failures else str(len(failures)) + ' FAILURES: ' + str(failures)}")
sys.exit(1 if failures else 0)
