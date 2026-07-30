"""Deterministic programming validator.

Checks a routine plan against the trainee's profile and the coaching principles in
principles.py. This is the component that makes "expert-grade" a verifiable property
rather than something the output merely sounds like: a plausible routine and a
well-programmed routine read the same, but only one passes these checks.

Violations are returned as structured objects, not prose, so they can be fed straight
back to the model as revision instructions and stored in `routine_reviews.violations`
for auditing.

Severity:
  error   — must be fixed before the routine is written to wger (safety, or a
            principle violated badly enough that the program is wrong)
  warning — worth revising, but the routine is still usable
  info    — observations that give the model useful context on a revision pass
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, asdict
from typing import Any, Iterable

from principles import (
    BALANCE_RULES,
    CONDITIONING_RULES,
    FATIGUE_PRIORITY,
    FUNDAMENTAL_PATTERNS,
    LARGE_MUSCLES,
    MUSCLE_TO_GROUP,
    REQUIRED_PATTERN_GROUPS,
    REQUIRED_PATTERNS,
    REQUIRED_PLANES,
    SESSION_TIME_MODEL,
    TRI_PLANAR_GOALS,
    VARIETY_RULES,
    VOLUME_CREDIT,
    VOLUME_EPSILON,
    ResolvedPrescription,
)

Severity = str


@dataclass
class Violation:
    code: str
    severity: Severity
    message: str
    # Where in the plan it applies, when applicable.
    location: str | None = None
    # Machine-readable specifics so the model gets numbers, not adjectives.
    detail: dict[str, Any] | None = None

    def as_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class TraineeContext:
    """Everything the validator needs to know about the trainee."""

    prescription: ResolvedPrescription
    experience_level: str = "novice"
    age: int | None = None
    sessions_per_week: int = 3
    minutes_per_session: int = 60
    available_equipment: set[str] | None = None  # None/empty = no restriction
    # Machine-actionable contraindications, keyed by kind.
    contraindications: dict[str, set[str]] | None = None
    dislikes: set[str] | None = None
    # Exercise ids used by the previous N routines, for the variety check.
    recent_exercise_ids: set[int] | None = None
    # Exercise ids logged recently, so the validator can flag a routine that
    # re-hammers what was just trained.
    recently_logged_exercise_ids: set[int] | None = None


def _iter_entries(plan: dict) -> Iterable[tuple[dict, dict, dict]]:
    """Yield (day, slot, entry) for every working entry in the plan."""
    for day in plan.get("days", []):
        if day.get("is_rest"):
            continue
        for slot in day.get("slots", []):
            for entry in slot.get("entries", []):
                yield day, slot, entry


def _working_entries(plan: dict) -> Iterable[tuple[dict, dict, dict]]:
    """Working sets only — warmups don't count toward volume."""
    for day, slot, entry in _iter_entries(plan):
        if entry.get("type", "normal") != "warmup":
            yield day, slot, entry


def validate(
    plan: dict,
    exercises: dict[int, dict],
    context: TraineeContext,
) -> list[Violation]:
    """Validate a routine plan. `exercises` maps sidecar exercise id -> record."""
    violations: list[Violation] = []
    v = violations.append

    # ------------------------------------------------------------------
    # 0. Referential integrity. Everything downstream assumes these hold.
    # ------------------------------------------------------------------
    missing = [
        entry["exercise_id"]
        for _, _, entry in _iter_entries(plan)
        if entry["exercise_id"] not in exercises
    ]
    if missing:
        v(Violation(
            code="unknown_exercise",
            severity="error",
            message=(
                f"{len(missing)} exercise id(s) are not in the exercise database: "
                f"{sorted(set(missing))[:10]}. Only use ids returned by search_exercises."
            ),
            detail={"exercise_ids": sorted(set(missing))},
        ))
        # Without valid exercises no other check is meaningful.
        return violations

    not_loggable = [
        entry["exercise_id"]
        for _, _, entry in _iter_entries(plan)
        if not exercises[entry["exercise_id"]].get("wger_exercise_id")
    ]
    if not_loggable:
        v(Violation(
            code="exercise_not_loggable",
            severity="error",
            message=(
                f"{len(set(not_loggable))} exercise(s) have not been imported into wger "
                "and therefore cannot be logged. Choose loggable exercises only."
            ),
            detail={"exercise_ids": sorted(set(not_loggable))},
        ))

    for _, _, entry in _iter_entries(plan):
        record = exercises[entry["exercise_id"]]
        claimed = entry.get("exercise_name")
        if claimed and claimed.strip().lower() != record["name"].strip().lower():
            v(Violation(
                code="exercise_name_mismatch",
                severity="error",
                message=(
                    f"exercise_id {entry['exercise_id']} is '{record['name']}' but the "
                    f"plan calls it '{claimed}'. The id is authoritative — this usually "
                    "means the wrong id was selected."
                ),
                detail={"exercise_id": entry["exercise_id"],
                        "actual": record["name"], "claimed": claimed},
            ))

    # ------------------------------------------------------------------
    # 1. Contraindications — hard safety boundary, checked first among rules.
    # ------------------------------------------------------------------
    violations.extend(_check_contraindications(plan, exercises, context))

    # ------------------------------------------------------------------
    # 2. Equipment availability
    # ------------------------------------------------------------------
    if context.available_equipment:
        unavailable: dict[str, list[str]] = defaultdict(list)
        for _, _, entry in _iter_entries(plan):
            record = exercises[entry["exercise_id"]]
            for field in ("primary_equipment", "secondary_equipment"):
                item = record.get(field)
                if item and item != "None" and item not in context.available_equipment:
                    unavailable[item].append(record["name"])
        for item, names in unavailable.items():
            v(Violation(
                code="equipment_unavailable",
                severity="error",
                message=(
                    f"'{item}' is not in the trainee's available equipment but is "
                    f"required by {len(names)} exercise(s), e.g. {names[0]}."
                ),
                detail={"equipment": item, "exercises": names[:5]},
            ))

    # ------------------------------------------------------------------
    # 3. Weekly volume per muscle group, in credited sets
    # ------------------------------------------------------------------
    volume = _weekly_volume(plan, exercises)
    prescription = context.prescription
    for muscle, sets in sorted(volume.items()):
        landmarks = (
            prescription.volume_large if muscle in LARGE_MUSCLES
            else prescription.volume_small
        )
        # Epsilon keeps a routine sitting exactly on a blended boundary from being
        # reported as violating it.
        if sets > landmarks.mrv + VOLUME_EPSILON:
            v(Violation(
                code="volume_above_mrv",
                severity="error",
                message=(
                    f"{muscle}: {sets:.1f} credited sets/week exceeds the maximum "
                    f"recoverable volume of {landmarks.mrv:.1f}. Reduce sets or move "
                    "some volume to another muscle group."
                ),
                detail={"muscle": muscle, "sets": round(sets, 1),
                        "mrv": round(landmarks.mrv, 1)},
            ))
        elif sets < landmarks.mev - VOLUME_EPSILON:
            v(Violation(
                code="volume_below_mev",
                severity="warning",
                message=(
                    f"{muscle}: {sets:.1f} credited sets/week is below the minimum "
                    f"effective volume of {landmarks.mev:.1f}. Expect little adaptation "
                    "unless this is deliberate maintenance."
                ),
                detail={"muscle": muscle, "sets": round(sets, 1),
                        "mev": round(landmarks.mev, 1)},
            ))

    # ------------------------------------------------------------------
    # 4. Frequency per muscle group
    # ------------------------------------------------------------------
    frequency = _muscle_frequency(plan, exercises)
    for muscle, days in sorted(frequency.items()):
        # Only hold trained muscles to the frequency target; a muscle that receives
        # only incidental tertiary work is not "trained".
        if volume.get(muscle, 0) < prescription.volume_small.mev:
            continue
        if days < prescription.min_frequency_per_muscle:
            v(Violation(
                code="frequency_below_target",
                severity="warning",
                message=(
                    f"{muscle} is trained on {days} day(s)/week; target is at least "
                    f"{prescription.min_frequency_per_muscle}. Spreading the same volume "
                    "across more sessions generally improves the response."
                ),
                detail={"muscle": muscle, "days": days,
                        "target": prescription.min_frequency_per_muscle},
            ))

    # ------------------------------------------------------------------
    # 5. Movement-pattern coverage and balance
    # ------------------------------------------------------------------
    violations.extend(_check_pattern_coverage(plan, exercises))
    violations.extend(_check_balance(plan, exercises))
    violations.extend(_check_planes(plan, exercises, prescription))

    # ------------------------------------------------------------------
    # 6. Exercise order within each session
    # ------------------------------------------------------------------
    violations.extend(_check_ordering(plan, exercises))

    # ------------------------------------------------------------------
    # 7. Compound share, rep/RIR/rest adherence
    # ------------------------------------------------------------------
    violations.extend(_check_compound_share(plan, exercises, prescription))
    violations.extend(_check_set_prescriptions(plan, exercises, prescription))

    # ------------------------------------------------------------------
    # 8. Schedule feasibility
    # ------------------------------------------------------------------
    violations.extend(_check_schedule(plan, context))

    # ------------------------------------------------------------------
    # 9. Concurrent-training interference
    # ------------------------------------------------------------------
    violations.extend(_check_conditioning(plan, exercises, prescription))

    # ------------------------------------------------------------------
    # 10. Variety versus previous routines and recent logs
    # ------------------------------------------------------------------
    violations.extend(_check_variety(plan, exercises, context))

    # ------------------------------------------------------------------
    # 11. Progression model matches experience level
    # ------------------------------------------------------------------
    violations.extend(_check_progression(plan, context))

    return violations


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def _check_contraindications(plan, exercises, context) -> list[Violation]:
    out: list[Violation] = []
    rules = context.contraindications or {}
    if not rules:
        return out

    for _, _, entry in _iter_entries(plan):
        record = exercises[entry["exercise_id"]]
        name = record["name"]

        if str(record["id"]) in rules.get("exercise", set()):
            out.append(Violation(
                code="contraindicated_exercise",
                severity="error",
                message=f"'{name}' is explicitly contraindicated for this trainee.",
                detail={"exercise_id": record["id"]},
            ))

        for pattern in record.get("movement_patterns") or []:
            if pattern in rules.get("movement_pattern", set()):
                out.append(Violation(
                    code="contraindicated_movement_pattern",
                    severity="error",
                    message=(
                        f"'{name}' uses the contraindicated movement pattern "
                        f"'{pattern}'. Select a different pattern for this slot."
                    ),
                    detail={"exercise_id": record["id"], "pattern": pattern},
                ))

        for field, kind in (
            ("primary_equipment", "equipment"),
            ("secondary_equipment", "equipment"),
            ("posture", "posture"),
            ("body_region", "body_region"),
            ("classification", "classification"),
        ):
            value = record.get(field)
            if value and value in rules.get(kind, set()):
                out.append(Violation(
                    code=f"contraindicated_{kind}",
                    severity="error",
                    message=(
                        f"'{name}' has {kind.replace('_', ' ')} '{value}', which is "
                        "contraindicated for this trainee."
                    ),
                    detail={"exercise_id": record["id"], kind: value},
                ))

        for plane in record.get("planes_of_motion") or []:
            if plane in rules.get("plane_of_motion", set()):
                out.append(Violation(
                    code="contraindicated_plane_of_motion",
                    severity="error",
                    message=f"'{name}' loads the contraindicated {plane}.",
                    detail={"exercise_id": record["id"], "plane": plane},
                ))

        ceilings = rules.get("max_difficulty", set())
        if ceilings:
            try:
                ceiling = min(int(c) for c in ceilings)
            except ValueError:
                ceiling = None
            rank = record.get("difficulty_rank")
            if ceiling is not None and rank is not None and rank > ceiling:
                out.append(Violation(
                    code="above_difficulty_ceiling",
                    severity="error",
                    message=(
                        f"'{name}' is difficulty tier {rank}, above this trainee's "
                        f"ceiling of {ceiling}."
                    ),
                    detail={"exercise_id": record["id"], "rank": rank, "ceiling": ceiling},
                ))
    return out


def _weekly_volume(plan, exercises) -> dict[str, float]:
    """Credited working sets per week, keyed on the coarse target muscle group.

    Volume landmarks are defined per target group (the 16 values in
    `target_muscle_group`), so that is the only granularity checked against them.
    Crediting the fine-grained muscle names as well would report the same problem
    twice ("Quadriceps" and "Quadriceps Femoris") — see `_fine_muscle_volume` for
    that view, which is informational only.

    An exercise contributes to a group when it is the target group (full credit), or
    when one of its secondary/tertiary muscles maps into that group (partial credit).
    """
    volume: dict[str, float] = defaultdict(float)
    for _, _, entry in _working_entries(plan):
        record = exercises[entry["exercise_id"]]
        sets = entry.get("sets", 0)
        group = record.get("target_muscle_group")
        if group:
            volume[group] += sets * VOLUME_CREDIT["prime"]
        # Indirect volume: a bench press trains triceps even though its target group
        # is Chest. Credited at the secondary/tertiary rate against the group that
        # muscle belongs to, when we can resolve it.
        for field, credit in (
            ("secondary_muscle", VOLUME_CREDIT["secondary"]),
            ("tertiary_muscle", VOLUME_CREDIT["tertiary"]),
        ):
            muscle = record.get(field)
            mapped = MUSCLE_TO_GROUP.get(muscle) if muscle else None
            if mapped and mapped != group:
                volume[mapped] += sets * credit
    return dict(volume)


def _fine_muscle_volume(plan, exercises) -> dict[str, float]:
    """Credited sets per specific muscle. Informational context for the model."""
    volume: dict[str, float] = defaultdict(float)
    for _, _, entry in _working_entries(plan):
        record = exercises[entry["exercise_id"]]
        sets = entry.get("sets", 0)
        for field, credit in (
            ("prime_mover_muscle", VOLUME_CREDIT["prime"]),
            ("secondary_muscle", VOLUME_CREDIT["secondary"]),
            ("tertiary_muscle", VOLUME_CREDIT["tertiary"]),
        ):
            muscle = record.get(field)
            if muscle:
                volume[muscle] += sets * credit
    return dict(volume)


def _muscle_frequency(plan, exercises) -> dict[str, int]:
    """Training days per week per target muscle group."""
    days_by_group: dict[str, set[int]] = defaultdict(set)
    for day, _, entry in _working_entries(plan):
        group = exercises[entry["exercise_id"]].get("target_muscle_group")
        if group:
            days_by_group[group].add(day.get("order", 0))
    return {g: len(d) for g, d in days_by_group.items()}


def _plan_patterns(plan, exercises) -> set[str]:
    found: set[str] = set()
    for _, _, entry in _working_entries(plan):
        found.update(exercises[entry["exercise_id"]].get("movement_patterns") or [])
    return found


def _check_pattern_coverage(plan, exercises) -> list[Violation]:
    out: list[Violation] = []
    present = _plan_patterns(plan, exercises)
    covered = {
        name for name, patterns in FUNDAMENTAL_PATTERNS.items() if present & patterns
    }

    for required in sorted(REQUIRED_PATTERNS - covered):
        expected = ", ".join(sorted(FUNDAMENTAL_PATTERNS[required]))
        out.append(Violation(
            code="missing_fundamental_pattern",
            severity="warning",
            message=(
                f"No {required.replace('_', ' ')} work across the week. Add an exercise "
                f"with one of these movement patterns: {expected}."
            ),
            detail={"pattern_group": required, "accepted_patterns": sorted(FUNDAMENTAL_PATTERNS[required])},
        ))

    for group, label in REQUIRED_PATTERN_GROUPS:
        if not (group & covered):
            out.append(Violation(
                code="missing_pattern_group",
                severity="warning",
                message=(
                    f"The week contains no {label} movements at all. Add at least one."
                ),
                detail={"needed_one_of": sorted(group)},
            ))
    return out


def _check_balance(plan, exercises) -> list[Violation]:
    out: list[Violation] = []
    push = pull = upper = lower = 0.0
    for _, _, entry in _working_entries(plan):
        record = exercises[entry["exercise_id"]]
        sets = entry.get("sets", 0)
        force = record.get("force_type")
        if force == "Push":
            push += sets
        elif force == "Pull":
            pull += sets
        elif force == "Push & Pull":
            push += sets / 2
            pull += sets / 2
        region = record.get("body_region")
        if region == "Upper Body":
            upper += sets
        elif region == "Lower Body":
            lower += sets

    low, high = BALANCE_RULES["push_pull_ratio"]
    if push and pull:
        ratio = pull / push
        if not low <= ratio <= high:
            out.append(Violation(
                code="push_pull_imbalance",
                severity="warning",
                message=(
                    f"Pull:push set ratio is {ratio:.2f}, outside the balanced range "
                    f"{low}-{high} ({pull:.0f} pull vs {push:.0f} push sets)."
                ),
                detail={"ratio": round(ratio, 2), "pull_sets": pull, "push_sets": push},
            ))
    elif push and not pull:
        out.append(Violation(
            code="push_pull_imbalance",
            severity="error",
            message=f"{push:.0f} pushing sets and zero pulling sets. Add pulling work.",
            detail={"push_sets": push, "pull_sets": 0},
        ))

    low, high = BALANCE_RULES["upper_lower_ratio"]
    if upper and lower:
        ratio = upper / lower
        if not low <= ratio <= high:
            out.append(Violation(
                code="upper_lower_imbalance",
                severity="warning",
                message=(
                    f"Upper:lower set ratio is {ratio:.2f}, outside {low}-{high} "
                    f"({upper:.0f} upper vs {lower:.0f} lower sets)."
                ),
                detail={"ratio": round(ratio, 2), "upper_sets": upper, "lower_sets": lower},
            ))
    return out


def _check_planes(plan, exercises, prescription) -> list[Violation]:
    if not (set(prescription.all_goals) & TRI_PLANAR_GOALS):
        return []
    present: set[str] = set()
    for _, _, entry in _working_entries(plan):
        present.update(exercises[entry["exercise_id"]].get("planes_of_motion") or [])
    missing = REQUIRED_PLANES - present
    if not missing:
        return []
    return [Violation(
        code="missing_plane_of_motion",
        severity="warning",
        message=(
            f"The week never loads the {', '.join(sorted(missing))}. For a general "
            "fitness or mobility goal, training all three planes is the substance of "
            "'functional' work — add a rotational or lateral movement."
        ),
        detail={"missing_planes": sorted(missing), "present_planes": sorted(present)},
    )]


def _check_ordering(plan, exercises) -> list[Violation]:
    out: list[Violation] = []
    for day in plan.get("days", []):
        if day.get("is_rest"):
            continue
        sequence = []
        for slot in sorted(day.get("slots", []), key=lambda s: s.get("order", 0)):
            for entry in slot.get("entries", []):
                if entry.get("type", "normal") == "warmup":
                    continue
                record = exercises[entry["exercise_id"]]
                sequence.append((slot.get("order", 0), record))

        # Compound-before-isolation.
        first_isolation = next(
            (i for i, (_, r) in enumerate(sequence) if r.get("mechanics") == "Isolation"),
            None,
        )
        if first_isolation is not None:
            later_compounds = [
                r["name"] for _, r in sequence[first_isolation + 1:]
                if r.get("mechanics") == "Compound"
            ]
            if later_compounds:
                out.append(Violation(
                    code="isolation_before_compound",
                    severity="warning",
                    message=(
                        f"On '{day.get('name')}', isolation work precedes "
                        f"{len(later_compounds)} compound movement(s) "
                        f"(e.g. {later_compounds[0]}). Compounds belong first, while "
                        "the trainee is fresh."
                    ),
                    location=day.get("name"),
                    detail={"compounds_after_isolation": later_compounds[:5]},
                ))

        # High-skill / high-fatigue classifications belong early.
        priority = {name: i for i, name in enumerate(FATIGUE_PRIORITY)}
        ranked = [
            (priority.get(r.get("classification") or "", len(FATIGUE_PRIORITY)), r)
            for _, r in sequence
        ]
        for i in range(1, len(ranked)):
            demanding, record = ranked[i]
            easier = min(r[0] for r in ranked[:i])
            # Only flag a clear inversion, not adjacent tiers.
            if demanding + 2 <= easier:
                out.append(Violation(
                    code="high_fatigue_exercise_late",
                    severity="info",
                    message=(
                        f"On '{day.get('name')}', '{record['name']}' "
                        f"({record.get('classification')}) is more demanding than "
                        "earlier work in the session. Consider moving it earlier."
                    ),
                    location=day.get("name"),
                    detail={"exercise": record["name"],
                            "classification": record.get("classification")},
                ))
                break
    return out


def _check_compound_share(plan, exercises, prescription) -> list[Violation]:
    compound = total = 0.0
    for _, _, entry in _working_entries(plan):
        record = exercises[entry["exercise_id"]]
        sets = entry.get("sets", 0)
        total += sets
        if record.get("mechanics") == "Compound":
            compound += sets
    if not total:
        return []
    share = compound / total
    if share >= prescription.min_compound_share:
        return []
    return [Violation(
        code="compound_share_low",
        severity="warning",
        message=(
            f"Compound movements are {share:.0%} of working sets; this trainee's goals "
            f"call for at least {prescription.min_compound_share:.0%}. Replace some "
            "isolation work with compounds."
        ),
        detail={"share": round(share, 2), "target": prescription.min_compound_share},
    )]


def _check_set_prescriptions(plan, exercises, prescription) -> list[Violation]:
    out: list[Violation] = []
    rep_low, rep_high = prescription.rep_range
    rir_low, rir_high = prescription.rir_range

    off_reps: list[str] = []
    off_rir: list[str] = []
    missing_rest: list[str] = []

    for day, _, entry in _working_entries(plan):
        record = exercises[entry["exercise_id"]]
        reps = entry.get("reps")
        if reps is not None and not rep_low <= reps <= rep_high:
            off_reps.append(f"{record['name']} ({reps} reps)")
        rir = entry.get("rir")
        if rir is not None and not rir_low <= rir <= rir_high:
            off_rir.append(f"{record['name']} (RIR {rir})")
        if entry.get("rest_seconds") is None:
            missing_rest.append(record["name"])

    if off_reps:
        out.append(Violation(
            code="reps_outside_goal_range",
            severity="warning",
            message=(
                f"{len(off_reps)} entry/entries fall outside the {rep_low}-{rep_high} "
                f"rep range implied by the trainee's goals, e.g. {off_reps[0]}."
            ),
            detail={"target_range": [rep_low, rep_high], "examples": off_reps[:5]},
        ))
    if off_rir:
        out.append(Violation(
            code="rir_outside_goal_range",
            severity="info",
            message=(
                f"{len(off_rir)} entry/entries prescribe reps-in-reserve outside the "
                f"{rir_low}-{rir_high} band, e.g. {off_rir[0]}."
            ),
            detail={"target_range": [rir_low, rir_high], "examples": off_rir[:5]},
        ))
    if missing_rest:
        out.append(Violation(
            code="rest_not_prescribed",
            severity="warning",
            message=(
                f"{len(missing_rest)} entry/entries have no rest period. Rest is part of "
                "the prescription, not an afterthought — specify it "
                f"(compounds {prescription.rest_seconds_compound[0]}-"
                f"{prescription.rest_seconds_compound[1]}s, isolation "
                f"{prescription.rest_seconds_isolation[0]}-"
                f"{prescription.rest_seconds_isolation[1]}s)."
            ),
            detail={"exercises": missing_rest[:5]},
        ))
    return out


def _check_schedule(plan, context) -> list[Violation]:
    out: list[Violation] = []
    training_days = [d for d in plan.get("days", []) if not d.get("is_rest")]

    if len(training_days) > context.sessions_per_week:
        out.append(Violation(
            code="too_many_sessions",
            severity="error",
            message=(
                f"The routine has {len(training_days)} training days but the trainee "
                f"has {context.sessions_per_week} available. Consolidate."
            ),
            detail={"planned": len(training_days), "available": context.sessions_per_week},
        ))

    model = SESSION_TIME_MODEL
    for day in training_days:
        sets = 0
        rest_total = 0
        exercise_count = 0
        for slot in day.get("slots", []):
            for entry in slot.get("entries", []):
                exercise_count += 1
                s = entry.get("sets", 0)
                sets += s
                # Rest is taken between sets, so one fewer interval than sets.
                rest_total += max(s - 1, 0) * (entry.get("rest_seconds") or 90)
        minutes = (
            model["warmup_minutes"]
            + (sets * model["seconds_per_set_execution"] + rest_total) / 60
            + exercise_count * model["transition_seconds_per_exercise"] / 60
        )
        budget = context.minutes_per_session + model["overrun_tolerance_minutes"]
        if minutes > budget:
            out.append(Violation(
                code="session_too_long",
                severity="warning",
                message=(
                    f"'{day.get('name')}' needs roughly {minutes:.0f} minutes but the "
                    f"trainee has {context.minutes_per_session}. Cut sets, shorten rest, "
                    "or superset accessory work."
                ),
                location=day.get("name"),
                detail={"estimated_minutes": round(minutes),
                        "budget_minutes": context.minutes_per_session},
            ))
    return out


def _check_conditioning(plan, exercises, prescription) -> list[Violation]:
    out: list[Violation] = []
    conditioning_classes = CONDITIONING_RULES["conditioning_classifications"]
    strength_focused = bool({"strength", "hypertrophy"} & set(prescription.all_goals))
    if not strength_focused:
        return out

    hard_days: list[str] = []
    for day in plan.get("days", []):
        if day.get("is_rest"):
            continue
        classes: set[str] = set()
        lower_sets = 0
        for slot in day.get("slots", []):
            for entry in slot.get("entries", []):
                record = exercises[entry["exercise_id"]]
                if record.get("classification"):
                    classes.add(record["classification"])
                if record.get("body_region") == "Lower Body" and \
                        record.get("mechanics") == "Compound":
                    lower_sets += entry.get("sets", 0)

        is_hard_conditioning = bool(classes & conditioning_classes)
        if is_hard_conditioning:
            hard_days.append(day.get("name", f"day {day.get('order')}"))
            if CONDITIONING_RULES["avoid_same_session_as_heavy_lower"] and lower_sets >= 6:
                out.append(Violation(
                    code="conditioning_interference",
                    severity="warning",
                    message=(
                        f"'{day.get('name')}' combines {lower_sets} sets of compound "
                        "lower-body work with high-intensity conditioning "
                        f"({', '.join(sorted(classes & conditioning_classes))}). With a "
                        "strength goal, separate these to limit the interference effect."
                    ),
                    location=day.get("name"),
                    detail={"lower_body_sets": lower_sets,
                            "conditioning": sorted(classes & conditioning_classes)},
                ))

    cap = CONDITIONING_RULES["max_hard_sessions_per_week_with_strength"]
    if len(hard_days) > cap:
        out.append(Violation(
            code="too_much_hard_conditioning",
            severity="warning",
            message=(
                f"{len(hard_days)} hard conditioning sessions per week exceeds the cap of "
                f"{cap} when strength is a goal ({', '.join(hard_days)})."
            ),
            detail={"sessions": hard_days, "cap": cap},
        ))
    return out


def _check_variety(plan, exercises, context) -> list[Violation]:
    out: list[Violation] = []
    used = {e["exercise_id"] for _, _, e in _working_entries(plan)}
    if not used:
        return out

    recent = context.recent_exercise_ids or set()
    if recent:
        exempt_patterns = VARIETY_RULES["continuity_exempt_patterns"]
        # Core lifts are meant to persist across blocks; rotating the squat every
        # month is bad programming, not variety.
        comparable = {
            eid for eid in used
            if not (set(exercises[eid].get("movement_patterns") or []) & exempt_patterns)
        }
        if comparable:
            fresh = comparable - recent
            share = len(fresh) / len(comparable)
            target = VARIETY_RULES["min_new_exercise_share"]
            if share < target:
                out.append(Violation(
                    code="insufficient_variety",
                    severity="info",
                    message=(
                        f"Only {share:.0%} of non-core exercises are new versus recent "
                        f"routines (target {target:.0%}). The database has 4,000+ "
                        "exercises — vary the accessory work."
                    ),
                    detail={"new_share": round(share, 2), "target": target,
                            "reused": sorted(comparable & recent)[:10]},
                ))

    logged = context.recently_logged_exercise_ids or set()
    overlap = used & logged
    if logged and len(overlap) / len(used) > 0.8:
        out.append(Violation(
            code="repeats_recent_training",
            severity="info",
            message=(
                f"{len(overlap)} of {len(used)} exercises were logged very recently. "
                "Consider whether this is intended continuity or unintended repetition."
            ),
            detail={"overlap_count": len(overlap), "total": len(used)},
        ))
    return out


def _check_progression(plan, context) -> list[Violation]:
    from principles import progression_for

    expected = progression_for(context.experience_level)["model"]
    actual = (plan.get("progression") or {}).get("model")
    if actual and actual != expected:
        return [Violation(
            code="progression_model_mismatch",
            severity="info",
            message=(
                f"Progression model '{actual}' does not match the usual choice for an "
                f"{context.experience_level} trainee ('{expected}'). Justify it in the "
                "rationale or switch."
            ),
            detail={"actual": actual, "expected": expected,
                    "experience_level": context.experience_level},
        )]
    return []


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def summarize(violations: list[Violation]) -> dict:
    counts = defaultdict(int)
    for violation in violations:
        counts[violation.severity] += 1
    return {
        "passed": counts["error"] == 0,
        "errors": counts["error"],
        "warnings": counts["warning"],
        "info": counts["info"],
        "violations": [v.as_dict() for v in violations],
    }


def as_revision_prompt(violations: list[Violation]) -> str:
    """Render violations as instructions the model can act on."""
    if not violations:
        return "The routine passed every programming check."
    by_severity: dict[str, list[Violation]] = defaultdict(list)
    for violation in violations:
        by_severity[violation.severity].append(violation)

    lines = [
        "The routine you produced failed programming review. Fix the items below and "
        "return a corrected routine plan in the same schema.",
        "",
    ]
    for severity in ("error", "warning", "info"):
        items = by_severity.get(severity)
        if not items:
            continue
        header = {
            "error": "MUST FIX (the routine cannot be used until these are resolved)",
            "warning": "SHOULD FIX (programming quality issues)",
            "info": "CONSIDER (observations)",
        }[severity]
        lines.append(f"## {header}")
        for item in items:
            where = f" [{item.location}]" if item.location else ""
            lines.append(f"- ({item.code}){where} {item.message}")
        lines.append("")
    return "\n".join(lines).strip()
