"""Tests for the programming validator.

Each test asserts that a specific, deliberately-broken routine produces the specific
violation code it should. The point is to prove the checks actually fire — a validator
that silently passes everything is worse than none, because it manufactures confidence.

Run: python -m pytest coaching/test_validate.py -q
     (or plain `python coaching/test_validate.py` for a dependency-free run)
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from principles import resolve_prescription  # noqa: E402
from validate import TraineeContext, validate, summarize, as_revision_prompt  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# Distinguishes "caller didn't specify a wger id" from "caller explicitly said None,
# meaning this exercise was never imported into wger".
_UNSET = object()


def exercise(
    eid: int,
    name: str,
    target: str,
    prime: str,
    *,
    mechanics: str = "Compound",
    patterns: list[str] | None = None,
    planes: list[str] | None = None,
    force: str = "Push",
    region: str = "Lower Body",
    equipment: str = "Barbell",
    classification: str = "Bodybuilding",
    posture: str = "Standing",
    difficulty_rank: int = 3,
    secondary: str | None = None,
    tertiary: str | None = None,
    wger_id: int | None | object = _UNSET,
) -> dict:
    return {
        "id": eid,
        "name": name,
        "target_muscle_group": target,
        "prime_mover_muscle": prime,
        "secondary_muscle": secondary,
        "tertiary_muscle": tertiary,
        "mechanics": mechanics,
        "movement_patterns": patterns or ["Knee Dominant"],
        "planes_of_motion": planes or ["Sagittal Plane"],
        "force_type": force,
        "body_region": region,
        "primary_equipment": equipment,
        "secondary_equipment": None,
        "classification": classification,
        "posture": posture,
        "difficulty_rank": difficulty_rank,
        "wger_exercise_id": (1000 + eid) if wger_id is _UNSET else wger_id,
    }


POOL = {
    1: exercise(1, "Barbell Back Squat", "Quadriceps", "Quadriceps Femoris",
                patterns=["Knee Dominant"], secondary="Gluteus Maximus"),
    2: exercise(2, "Barbell Deadlift", "Hamstrings", "Biceps Femoris",
                patterns=["Hip Hinge"], force="Pull", secondary="Gluteus Maximus"),
    3: exercise(3, "Barbell Bench Press", "Chest", "Pectoralis Major",
                patterns=["Horizontal Push"], region="Upper Body",
                secondary="Triceps Brachii"),
    4: exercise(4, "Pull Up", "Back", "Latissimus Dorsi", patterns=["Vertical Pull"],
                force="Pull", region="Upper Body", equipment="Pull Up Bar",
                secondary="Biceps Brachii"),
    5: exercise(5, "Bodyweight Plank", "Abdominals", "Rectus Abdominis",
                patterns=["Anti-Extension"], region="Core", equipment="Bodyweight",
                mechanics="Isolation", force="Other"),
    6: exercise(6, "Dumbbell Bicep Curl", "Biceps", "Biceps Brachii",
                patterns=["Elbow Flexion"], mechanics="Isolation", force="Pull",
                region="Upper Body", equipment="Dumbbell"),
    7: exercise(7, "Cable Woodchop", "Abdominals", "Obliques",
                patterns=["Rotational"], planes=["Transverse Plane"], region="Core",
                equipment="Cable", mechanics="Compound", force="Pull"),
    8: exercise(8, "Kettlebell Lateral Lunge", "Quadriceps", "Quadriceps Femoris",
                patterns=["Knee Dominant"], planes=["Frontal Plane"],
                equipment="Kettlebell"),
    9: exercise(9, "Kettlebell Swing", "Glutes", "Gluteus Maximus",
                patterns=["Hip Hinge"], equipment="Kettlebell",
                classification="Ballistics", force="Pull"),
    10: exercise(10, "Box Jump", "Quadriceps", "Quadriceps Femoris",
                 patterns=["Knee Dominant"], equipment="Plyo Box",
                 classification="Plyometric"),
    11: exercise(11, "Overhead Press", "Shoulders", "Anterior Deltoids",
                 patterns=["Vertical Push"], region="Upper Body",
                 secondary="Triceps Brachii"),
    # Deliberately not imported into wger, so it cannot be logged.
    99: exercise(99, "Unimported Exercise", "Chest", "Pectoralis Major", wger_id=None),
}


def entry(eid: int, sets: int = 3, reps: int = 10, rest: int = 120, **kw) -> dict:
    return {"exercise_id": eid, "sets": sets, "reps": reps, "rest_seconds": rest, **kw}


def day(order: int, name: str, entries: list[dict], is_rest: bool = False) -> dict:
    return {
        "order": order,
        "name": name,
        "is_rest": is_rest,
        "slots": [
            {"order": i + 1, "entries": [e]} for i, e in enumerate(entries)
        ],
    }


def plan(days: list[dict], **kw) -> dict:
    base = {
        "name": "Test Routine",
        "description": "A routine used by the validator tests.",
        "weeks": 6,
        "progression": {
            "model": "double_progression",
            "detail": "Work to the top of the rep range, then add 2.5kg and reset.",
        },
        "rationale": "Built for testing the deterministic programming validator only.",
        "days": days,
    }
    base.update(kw)
    return base


def context(**kw) -> TraineeContext:
    defaults = {
        "prescription": resolve_prescription(
            [("general_fitness", 1), ("strength", 1), ("fat_loss", 2)], age=30
        ),
        "experience_level": "intermediate",
        "age": 30,
        "sessions_per_week": 4,
        "minutes_per_session": 75,
    }
    defaults.update(kw)
    return TraineeContext(**defaults)


def codes(violations) -> set[str]:
    return {v.code for v in violations}


# ---------------------------------------------------------------------------
# A reasonable baseline routine, used to prove the validator isn't just noisy
# ---------------------------------------------------------------------------

def balanced_plan() -> dict:
    return plan([
        day(1, "Lower A", [
            entry(1, sets=4, reps=6, rest=210),
            entry(2, sets=3, reps=6, rest=210),
            entry(8, sets=3, reps=10, rest=120),
            entry(5, sets=3, reps=12, rest=60),
        ]),
        day(2, "Upper A", [
            entry(3, sets=4, reps=6, rest=210),
            entry(4, sets=4, reps=8, rest=150),
            entry(11, sets=3, reps=8, rest=150),
            entry(6, sets=3, reps=12, rest=60),
        ]),
        day(3, "Lower B", [
            entry(9, sets=4, reps=12, rest=120),
            entry(2, sets=3, reps=6, rest=210),
            entry(8, sets=3, reps=10, rest=120),
            entry(7, sets=3, reps=12, rest=60),
        ]),
        day(4, "Upper B", [
            entry(4, sets=4, reps=8, rest=150),
            entry(3, sets=3, reps=8, rest=150),
            entry(11, sets=3, reps=10, rest=120),
            entry(5, sets=3, reps=12, rest=60),
        ]),
    ])


def test_balanced_plan_has_no_errors():
    violations = validate(balanced_plan(), POOL, context())
    result = summarize(violations)
    assert result["passed"], (
        "a reasonable routine should produce no errors, got: "
        f"{[v['code'] for v in result['violations'] if v['severity'] == 'error']}"
    )


# ---------------------------------------------------------------------------
# Referential integrity
# ---------------------------------------------------------------------------

def test_unknown_exercise_id_is_an_error():
    bad = plan([day(1, "Day", [entry(1234)])])
    assert "unknown_exercise" in codes(validate(bad, POOL, context()))


def test_unimported_exercise_cannot_be_logged():
    bad = plan([day(1, "Day", [entry(99)])])
    assert "exercise_not_loggable" in codes(validate(bad, POOL, context()))


def test_name_mismatch_catches_wrong_id():
    bad = plan([day(1, "Day", [
        {**entry(1), "exercise_name": "Barbell Bench Press"},
    ])])
    assert "exercise_name_mismatch" in codes(validate(bad, POOL, context()))


# ---------------------------------------------------------------------------
# Contraindications — the safety boundary
# ---------------------------------------------------------------------------

def test_contraindicated_movement_pattern_is_an_error():
    ctx = context(contraindications={"movement_pattern": {"Vertical Push"}})
    result = validate(balanced_plan(), POOL, ctx)
    assert "contraindicated_movement_pattern" in codes(result)
    offending = [v for v in result if v.code == "contraindicated_movement_pattern"]
    assert all(v.severity == "error" for v in offending)


def test_contraindicated_exercise_is_an_error():
    ctx = context(contraindications={"exercise": {"3"}})
    assert "contraindicated_exercise" in codes(validate(balanced_plan(), POOL, ctx))


def test_contraindicated_equipment_is_an_error():
    ctx = context(contraindications={"equipment": {"Barbell"}})
    assert "contraindicated_equipment" in codes(validate(balanced_plan(), POOL, ctx))


def test_difficulty_ceiling_is_enforced():
    ctx = context(contraindications={"max_difficulty": {"2"}})
    assert "above_difficulty_ceiling" in codes(validate(balanced_plan(), POOL, ctx))


def test_no_contraindications_means_no_contraindication_violations():
    result = codes(validate(balanced_plan(), POOL, context()))
    assert not any(c.startswith("contraindicated") for c in result)


# ---------------------------------------------------------------------------
# Equipment
# ---------------------------------------------------------------------------

def test_unavailable_equipment_is_an_error():
    ctx = context(available_equipment={"Bodyweight", "Kettlebell"})
    result = validate(balanced_plan(), POOL, ctx)
    assert "equipment_unavailable" in codes(result)


def test_empty_equipment_set_imposes_no_restriction():
    ctx = context(available_equipment=set())
    assert "equipment_unavailable" not in codes(validate(balanced_plan(), POOL, ctx))


# ---------------------------------------------------------------------------
# Volume
# ---------------------------------------------------------------------------

def test_excessive_volume_exceeds_mrv():
    # 10 sets x 6 squat-pattern days, far beyond any recoverable quad volume.
    bad = plan([
        day(i, f"Day {i}", [entry(1, sets=10), entry(8, sets=10)])
        for i in range(1, 7)
    ])
    result = validate(bad, POOL, context(sessions_per_week=6))
    assert "volume_above_mrv" in codes(result)
    assert summarize(result)["passed"] is False


def test_trivial_volume_falls_below_mev():
    thin = plan([day(1, "Day", [entry(1, sets=1, reps=8)])])
    assert "volume_below_mev" in codes(validate(thin, POOL, context()))


# ---------------------------------------------------------------------------
# Balance and coverage
# ---------------------------------------------------------------------------

def test_all_push_no_pull_is_an_error():
    bad = plan([
        day(1, "Push A", [entry(3, sets=5), entry(11, sets=5)]),
        day(2, "Push B", [entry(3, sets=5), entry(11, sets=5)]),
    ])
    result = validate(bad, POOL, context())
    assert "push_pull_imbalance" in codes(result)
    assert any(
        v.severity == "error" for v in result if v.code == "push_pull_imbalance"
    )


def test_missing_hinge_pattern_is_flagged():
    bad = plan([
        day(1, "Day 1", [entry(1, sets=4), entry(3, sets=4), entry(5, sets=3)]),
        day(2, "Day 2", [entry(1, sets=4), entry(4, sets=4), entry(5, sets=3)]),
    ])
    assert "missing_fundamental_pattern" in codes(validate(bad, POOL, context()))


def test_sagittal_only_plan_is_flagged_for_general_fitness():
    # Nothing in the frontal or transverse plane.
    bad = plan([
        day(1, "Day 1", [entry(1, sets=4), entry(2, sets=4), entry(5, sets=3)]),
        day(2, "Day 2", [entry(3, sets=4), entry(4, sets=4), entry(5, sets=3)]),
    ])
    result = validate(bad, POOL, context())
    assert "missing_plane_of_motion" in codes(result)


def test_tri_planar_check_skipped_for_pure_strength_goal():
    ctx = context(prescription=resolve_prescription([("strength", 1)], age=30))
    bad = plan([
        day(1, "Day 1", [entry(1, sets=4), entry(2, sets=4)]),
        day(2, "Day 2", [entry(3, sets=4), entry(4, sets=4)]),
    ])
    assert "missing_plane_of_motion" not in codes(validate(bad, POOL, ctx))


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------

def test_isolation_before_compound_is_flagged():
    bad = plan([day(1, "Muddled", [
        entry(6, sets=3),   # bicep curl, isolation
        entry(1, sets=4),   # squat, compound — should have come first
        entry(2, sets=3),
    ])])
    assert "isolation_before_compound" in codes(validate(bad, POOL, context()))


def test_correct_ordering_is_not_flagged():
    good = plan([day(1, "Ordered", [
        entry(1, sets=4), entry(2, sets=3), entry(6, sets=3),
    ])])
    assert "isolation_before_compound" not in codes(validate(good, POOL, context()))


# ---------------------------------------------------------------------------
# Schedule feasibility
# ---------------------------------------------------------------------------

def test_more_days_than_available_is_an_error():
    bad = plan([day(i, f"Day {i}", [entry(1, sets=3)]) for i in range(1, 7)])
    result = validate(bad, POOL, context(sessions_per_week=3))
    assert "too_many_sessions" in codes(result)


def test_overlong_session_is_flagged():
    # 8 exercises x 5 sets x 240s rest cannot fit in 45 minutes.
    bad = plan([day(1, "Marathon", [
        entry(eid, sets=5, rest=240) for eid in (1, 2, 3, 4, 11, 8, 7, 5)
    ])])
    result = validate(bad, POOL, context(minutes_per_session=45))
    assert "session_too_long" in codes(result)


def test_rest_periods_must_be_prescribed():
    bad = plan([day(1, "Day", [
        {"exercise_id": 1, "sets": 4, "reps": 8},  # no rest_seconds
    ])])
    assert "rest_not_prescribed" in codes(validate(bad, POOL, context()))


# ---------------------------------------------------------------------------
# Concurrent training
# ---------------------------------------------------------------------------

def test_conditioning_stacked_on_heavy_lower_day_is_flagged():
    bad = plan([day(1, "Lower + HIIT", [
        entry(1, sets=5, reps=5, rest=210),   # heavy compound lower
        entry(2, sets=4, reps=5, rest=210),   # heavy compound lower
        entry(10, sets=4, reps=5, rest=120),  # Plyometric — hard conditioning
    ])])
    ctx = context(prescription=resolve_prescription(
        [("strength", 1), ("fat_loss", 2)], age=30
    ))
    assert "conditioning_interference" in codes(validate(bad, POOL, ctx))


def test_too_many_hard_conditioning_sessions_is_flagged():
    bad = plan([
        day(i, f"Cond {i}", [entry(9, sets=4), entry(10, sets=3), entry(4, sets=3)])
        for i in range(1, 6)
    ])
    ctx = context(
        prescription=resolve_prescription([("strength", 1)], age=30),
        sessions_per_week=5,
    )
    assert "too_much_hard_conditioning" in codes(validate(bad, POOL, ctx))


# ---------------------------------------------------------------------------
# Variety
# ---------------------------------------------------------------------------

def test_recycling_recent_exercises_is_flagged():
    ctx = context(recent_exercise_ids={3, 4, 5, 6, 7, 8, 9, 11})
    assert "insufficient_variety" in codes(validate(balanced_plan(), POOL, ctx))


def test_core_lifts_are_exempt_from_the_variety_check():
    # Squat and deadlift reused, everything else fresh: should not be flagged.
    p = plan([
        day(1, "Lower", [entry(1, sets=4, reps=6, rest=210),
                         entry(2, sets=3, reps=6, rest=210),
                         entry(7, sets=3, reps=12, rest=60)]),
        day(2, "Upper", [entry(3, sets=4, reps=8, rest=180),
                         entry(4, sets=4, reps=8, rest=150),
                         entry(11, sets=3, reps=10, rest=120)]),
    ])
    ctx = context(recent_exercise_ids={1, 2})
    assert "insufficient_variety" not in codes(validate(p, POOL, ctx))


# ---------------------------------------------------------------------------
# Progression
# ---------------------------------------------------------------------------

def test_progression_model_mismatch_is_flagged():
    p = plan([day(1, "Day", [entry(1, sets=4)])],
             progression={"model": "autoregulated_rir",
                          "detail": "Autoregulate by RIR each session as needed."})
    ctx = context(experience_level="novice")
    assert "progression_model_mismatch" in codes(validate(p, POOL, ctx))


# ---------------------------------------------------------------------------
# Goal resolution
# ---------------------------------------------------------------------------

def test_fat_loss_plus_strength_emits_a_conflict_note():
    prescription = resolve_prescription(
        [("strength", 1), ("fat_loss", 1), ("general_fitness", 2)], age=30
    )
    assert any("energy balance" in n for n in prescription.notes), prescription.notes


def test_age_reduces_the_volume_ceiling():
    young = resolve_prescription([("hypertrophy", 1)], age=25)
    older = resolve_prescription([("hypertrophy", 1)], age=62)
    assert older.volume_large.mrv < young.volume_large.mrv
    assert older.notes


def test_unknown_goal_falls_back_rather_than_raising():
    prescription = resolve_prescription([("competitive_yodelling", 1)])
    assert prescription.all_goals == ["general_fitness"]


def test_primary_goal_drives_priority():
    prescription = resolve_prescription([("strength", 1), ("fat_loss", 3)])
    assert prescription.primary_goals == ["strength"]


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def test_revision_prompt_lists_errors_first():
    bad = plan([day(1, "Day", [entry(99)])])
    text = as_revision_prompt(validate(bad, POOL, context()))
    assert "MUST FIX" in text


def test_revision_prompt_when_clean():
    assert "passed every programming check" in as_revision_prompt([])


# ---------------------------------------------------------------------------
# Dependency-free runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        (name, obj) for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    failures = []
    for name, test in tests:
        try:
            test()
            print(f"  PASS  {name}")
        except AssertionError as exc:
            failures.append((name, exc))
            print(f"  FAIL  {name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures.append((name, exc))
            print(f"  ERROR {name}: {type(exc).__name__}: {exc}")

    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    if failures:
        sys.exit(1)
