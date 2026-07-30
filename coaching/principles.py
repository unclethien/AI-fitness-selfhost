"""Strength & conditioning programming principles, as tunable configuration.

This is the "certified coach knowledge" made operational. It is deliberately *data*
rather than prose in a system prompt, for three reasons:

  1. A prompt can be ignored by the model; a config drives a deterministic check.
  2. You can tune these numbers as you learn what works for you, without touching
     any prompt or code.
  3. Every number is traceable to a source, so disagreements are about evidence.

Sources for the ranges below (all mainstream S&C consensus, not fringe):
  - NSCA, *Essentials of Strength Training and Conditioning*, 4th ed. — set/rep/rest
    prescriptions by training goal; exercise-order guidelines.
  - ACSM position stand on progression models in resistance training.
  - Schoenfeld et al. (2016), *J Sports Sci* — meta-analysis on training frequency;
    basis for the >=2x/week per muscle group target.
  - Schoenfeld et al. (2017), *J Strength Cond Res* — dose-response of weekly volume.
  - Israetel et al., Renaissance Periodization — MEV / MAV / MRV volume-landmark
    framing (the specific numbers here are the widely-published middle of the range).
  - Hickson (1980) and subsequent concurrent-training literature — the interference
    effect that shapes CONDITIONING_RULES.
  - Helms et al. (2016) — RIR-based autoregulation.

Nothing here is medical advice, and nothing here assesses injury. Trainee-declared
contraindications are enforced separately, in validate.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Muscle size classes
#
# Large muscles both tolerate and require more direct weekly volume than small
# ones, and small muscles accumulate meaningful indirect volume from compounds.
# Keyed on the `target_muscle_group` values present in the exercise database.
# ---------------------------------------------------------------------------

LARGE_MUSCLES = {"Quadriceps", "Back", "Chest", "Glutes", "Hamstrings", "Shoulders"}
SMALL_MUSCLES = {
    "Biceps", "Triceps", "Calves", "Forearms", "Abdominals", "Trapezius",
    "Hip Flexors", "Abductors", "Adductors", "Shins",
}

# Fractional volume crediting: a set of Barbell Squat gives the quadriceps (prime
# mover) full credit, the glutes (secondary) half, the hamstrings (tertiary) a
# quarter. This is a working convention rather than a measured constant — it exists
# so indirect volume is not ignored entirely, which is the more common error.
VOLUME_CREDIT = {"prime": 1.0, "secondary": 0.5, "tertiary": 0.25}

# Maps the exercise database's specific muscle names onto the coarse target groups
# that the volume landmarks are keyed on. Needed so indirect volume lands in the
# right bucket: a bench press (target group Chest) still credits triceps work.
#
# Muscles with no sensible target-group home (deep rotator cuff, tibialis, TFL) are
# intentionally absent — they receive no landmark check rather than a misleading one.
MUSCLE_TO_GROUP: dict[str, str] = {
    "Quadriceps Femoris": "Quadriceps",
    "Rectus Femoris": "Quadriceps",
    "Vastus Mediais": "Quadriceps",
    "Biceps Femoris": "Hamstrings",
    "Gluteus Maximus": "Glutes",
    "Gluteus Medius": "Glutes",
    "Gluteus Minimus": "Glutes",
    "Gastrocnemius": "Calves",
    "Soleus": "Calves",
    "Tibialis Anterior": "Shins",
    "Extensor Digitorum Longus": "Shins",
    "Extensor Hallucis Longus": "Shins",
    "Pectoralis Major": "Chest",
    "Serratus Anterior": "Chest",
    "Latissimus Dorsi": "Back",
    "Teres Major": "Back",
    "Teres Minor": "Back",
    "Rhomboids": "Back",
    "Erector Spinae": "Back",
    "Upper Trapezius": "Trapezius",
    "Trapezius": "Trapezius",
    "Levator Scapulae": "Trapezius",
    "Anterior Deltoids": "Shoulders",
    "Lateral Deltoids": "Shoulders",
    "Medial Deltoids": "Shoulders",
    "Posterior Deltoids": "Shoulders",
    "Infraspinatus": "Shoulders",
    "Supraspinatus": "Shoulders",
    "Subscapularis": "Shoulders",
    "Biceps Brachii": "Biceps",
    "Brachialis": "Biceps",
    "Triceps Brachii": "Triceps",
    "Anconeus": "Triceps",
    "Brachioradialis": "Forearms",
    "Flexor Carpi Radialis": "Forearms",
    "Rectus Abdominis": "Abdominals",
    "Obliques": "Abdominals",
    "Transverse Abdominis": "Abdominals",
    "Iliopsoas": "Hip Flexors",
    "Adductor Magnus": "Adductors",
    "Tensor Fasciae Latae": "Abductors",
}

# Volume comparisons run on blended floating-point landmarks, so a routine sitting
# exactly on a boundary would otherwise produce "17.0 sets exceeds the maximum of 17".
VOLUME_EPSILON = 0.5


@dataclass(frozen=True)
class VolumeLandmarks:
    """Weekly set counts per muscle group, in credited sets."""

    mev: float  # minimum effective volume — below this, expect little adaptation
    mav: float  # maximum adaptive volume — the productive target band's top
    mrv: float  # maximum recoverable volume — above this, expect accumulated fatigue


@dataclass(frozen=True)
class GoalPrescription:
    """How a single training goal wants sets, reps, intensity and rest configured."""

    volume_large: VolumeLandmarks
    volume_small: VolumeLandmarks
    rep_range: tuple[int, int]
    rir_range: tuple[int, int]          # reps in reserve on working sets
    rest_seconds_compound: tuple[int, int]
    rest_seconds_isolation: tuple[int, int]
    min_frequency_per_muscle: int       # sessions per week touching each muscle group
    # Fraction of weekly working sets that should come from compound movements.
    min_compound_share: float


# ---------------------------------------------------------------------------
# Per-goal prescriptions
# ---------------------------------------------------------------------------

GOAL_PRESCRIPTIONS: dict[str, GoalPrescription] = {
    "strength": GoalPrescription(
        # Strength work is lower-volume/higher-intensity than hypertrophy work.
        volume_large=VolumeLandmarks(mev=6, mav=14, mrv=18),
        volume_small=VolumeLandmarks(mev=2, mav=8, mrv=12),
        rep_range=(3, 6),
        rir_range=(2, 4),
        rest_seconds_compound=(180, 300),
        rest_seconds_isolation=(90, 180),
        min_frequency_per_muscle=2,
        min_compound_share=0.70,
    ),
    "hypertrophy": GoalPrescription(
        volume_large=VolumeLandmarks(mev=8, mav=20, mrv=25),
        volume_small=VolumeLandmarks(mev=4, mav=14, mrv=20),
        rep_range=(6, 15),
        rir_range=(1, 3),
        rest_seconds_compound=(120, 180),
        rest_seconds_isolation=(60, 120),
        min_frequency_per_muscle=2,
        min_compound_share=0.50,
    ),
    "general_fitness": GoalPrescription(
        volume_large=VolumeLandmarks(mev=6, mav=14, mrv=20),
        volume_small=VolumeLandmarks(mev=3, mav=10, mrv=15),
        rep_range=(8, 15),
        rir_range=(2, 3),
        rest_seconds_compound=(90, 180),
        rest_seconds_isolation=(45, 90),
        min_frequency_per_muscle=2,
        min_compound_share=0.55,
    ),
    "fat_loss": GoalPrescription(
        # Fat loss is driven by energy balance, not by the resistance program. The
        # programming job here is to PRESERVE muscle and strength in a deficit, which
        # means holding intensity and not slashing volume. Cutting sets and adding
        # "toning" work is the classic error this config prevents.
        volume_large=VolumeLandmarks(mev=6, mav=14, mrv=18),
        volume_small=VolumeLandmarks(mev=3, mav=10, mrv=14),
        rep_range=(6, 12),
        rir_range=(1, 3),
        rest_seconds_compound=(90, 180),
        rest_seconds_isolation=(45, 90),
        min_frequency_per_muscle=2,
        min_compound_share=0.60,
    ),
    "endurance": GoalPrescription(
        volume_large=VolumeLandmarks(mev=4, mav=12, mrv=18),
        volume_small=VolumeLandmarks(mev=2, mav=8, mrv=12),
        rep_range=(12, 25),
        rir_range=(2, 4),
        rest_seconds_compound=(45, 90),
        rest_seconds_isolation=(30, 60),
        min_frequency_per_muscle=2,
        min_compound_share=0.50,
    ),
    "mobility": GoalPrescription(
        volume_large=VolumeLandmarks(mev=2, mav=8, mrv=12),
        volume_small=VolumeLandmarks(mev=2, mav=6, mrv=10),
        rep_range=(8, 20),
        rir_range=(3, 5),
        rest_seconds_compound=(45, 90),
        rest_seconds_isolation=(30, 60),
        min_frequency_per_muscle=3,
        min_compound_share=0.30,
    ),
    "skill": GoalPrescription(
        # Skill work is practised fresh and frequently, well short of failure.
        volume_large=VolumeLandmarks(mev=4, mav=10, mrv=15),
        volume_small=VolumeLandmarks(mev=2, mav=8, mrv=12),
        rep_range=(1, 5),
        rir_range=(3, 5),
        rest_seconds_compound=(120, 300),
        rest_seconds_isolation=(60, 120),
        min_frequency_per_muscle=3,
        min_compound_share=0.60,
    ),
}


# ---------------------------------------------------------------------------
# Progression models by experience level
#
# Matching the progression model to training age is one of the clearest markers of
# competent programming: a novice on autoregulated blocks under-recovers on
# decisions they can't yet make, and an advanced lifter on linear progression stalls
# within weeks.
# ---------------------------------------------------------------------------

PROGRESSION_MODELS: dict[str, dict[str, object]] = {
    "novice": {
        "model": "linear",
        "detail": (
            "Add a fixed increment to the same set/rep scheme each session while form "
            "holds. Small increments on upper-body lifts, larger on lower-body."
        ),
        "increment_kg_upper": 1.25,
        "increment_kg_lower": 2.5,
        "deload_every_weeks": 8,
    },
    "intermediate": {
        "model": "double_progression",
        "detail": (
            "Work up within the prescribed rep range at a fixed load. Once the top of "
            "the range is reached on every set, add load and return to the bottom."
        ),
        "increment_kg_upper": 2.5,
        "increment_kg_lower": 5.0,
        "deload_every_weeks": 6,
    },
    "advanced": {
        "model": "autoregulated_rir",
        "detail": (
            "Load is driven by target reps-in-reserve per session, with weekly volume "
            "accumulating from MEV toward MRV across the block, then a deload."
        ),
        "increment_kg_upper": 2.5,
        "increment_kg_lower": 5.0,
        "deload_every_weeks": 4,
    },
}


# ---------------------------------------------------------------------------
# Age adjustments
#
# Recovery capacity declines with age, so the top of the volume band comes down and
# high-impact work gets flagged. Deliberately conservative and easy to override —
# chronological age is a weak proxy for training capacity.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AgeAdjustment:
    mrv_multiplier: float
    note: str


AGE_ADJUSTMENTS: list[tuple[int, AgeAdjustment]] = [
    (40, AgeAdjustment(1.00, "")),
    (50, AgeAdjustment(0.90, "Slightly reduced volume ceiling; prioritize recovery between hard sessions.")),
    (60, AgeAdjustment(0.80, "Reduced volume ceiling; favour lower-impact variants and longer warm-ups.")),
    (200, AgeAdjustment(0.70, "Conservative volume ceiling; emphasize joint-friendly loading and balance work.")),
]


def age_adjustment(age: int | None) -> AgeAdjustment:
    if age is None:
        return AgeAdjustment(1.00, "")
    for threshold, adjustment in AGE_ADJUSTMENTS:
        if age < threshold:
            return adjustment
    return AGE_ADJUSTMENTS[-1][1]


# ---------------------------------------------------------------------------
# Movement balance
#
# These are the checks that distinguish a well-rounded program from a collection of
# favourite lifts, and they are only checkable because the exercise database carries
# movement pattern, plane and force-type attributes.
# ---------------------------------------------------------------------------

# Antagonist pairs that should stay roughly balanced across a training week.
# Ratio is (pull sets / push sets) and similar; wide tolerance because a deliberate
# emphasis block is legitimate — this catches gross imbalance, not nuance.
BALANCE_RULES = {
    "push_pull_ratio": (0.6, 1.7),
    "upper_lower_ratio": (0.5, 2.0),
}

# Across a week, a program claiming general fitness should not live entirely in the
# sagittal plane. This is the substantive content of "functional" training.
REQUIRED_PLANES = {"Sagittal Plane", "Frontal Plane", "Transverse Plane"}
# Goals for which tri-planar coverage is expected rather than optional.
TRI_PLANAR_GOALS = {"general_fitness", "mobility", "skill"}

# Fundamental patterns a general program should cover over a week. Named to match
# the `movement_patterns` values in the exercise database.
FUNDAMENTAL_PATTERNS = {
    "squat_pattern": {"Knee Dominant"},
    "hinge_pattern": {"Hip Hinge", "Hip Extension"},
    "vertical_push": {"Vertical Push"},
    "horizontal_push": {"Horizontal Push"},
    "vertical_pull": {"Vertical Pull"},
    "horizontal_pull": {"Horizontal Pull"},
    "core": {
        "Anti-Extension", "Anti-Rotational", "Anti-Lateral Flexion",
        "Spinal Flexion", "Rotational",
    },
    "loaded_carry": {"Loaded Carry"},
}

# Patterns that must appear for a program to be considered balanced. Loaded carries
# and explicit rotation are valuable but not mandatory in a 2-3 day week.
REQUIRED_PATTERNS = {
    "squat_pattern", "hinge_pattern", "core",
}
# One of each pair is enough — a week with horizontal pressing but no overhead work
# is fine; a week with no pressing at all is not.
REQUIRED_PATTERN_GROUPS = [
    ({"vertical_push", "horizontal_push"}, "pressing"),
    ({"vertical_pull", "horizontal_pull"}, "pulling"),
]


# ---------------------------------------------------------------------------
# Exercise ordering
# ---------------------------------------------------------------------------

# High-skill, high-fatigue classifications belong early in a session while the
# trainee is fresh. Ordered most- to least-demanding.
FATIGUE_PRIORITY = [
    "Olympic Weightlifting",
    "Plyometric",
    "Ballistics",
    "Powerlifting",
    "Grinds",
    "Calisthenics",
    "Bodybuilding",
    "Balance",
    "Postural",
    "Mobility",
    "Animal Flow",
]


# ---------------------------------------------------------------------------
# Concurrent training
#
# The interference effect: hard aerobic/glycolytic conditioning degrades lower-body
# strength adaptation when stacked onto the same session or adjacent to it. Relevant
# to a "general fitness + fat loss + strength" trainee, who is the exact case where
# people bolt conditioning onto everything and stall.
# ---------------------------------------------------------------------------

CONDITIONING_RULES = {
    # Classifications that count as high-intensity conditioning for interference.
    "conditioning_classifications": {"Ballistics", "Plyometric"},
    # Cap on hard conditioning sessions per week when strength is a priority goal.
    "max_hard_sessions_per_week_with_strength": 3,
    # A session should not be both a heavy lower-body strength session and a hard
    # conditioning session.
    "avoid_same_session_as_heavy_lower": True,
}


# ---------------------------------------------------------------------------
# Session-length feasibility
#
# A routine the trainee cannot finish in the time they have is a bad routine
# regardless of how well periodized it is.
# ---------------------------------------------------------------------------

SESSION_TIME_MODEL = {
    "seconds_per_set_execution": 45,   # time under tension plus setup
    "warmup_minutes": 8,
    "transition_seconds_per_exercise": 60,
    # Tolerance before flagging: routines rarely land exactly on the budget.
    "overrun_tolerance_minutes": 10,
}


# ---------------------------------------------------------------------------
# Variety / anti-staleness
# ---------------------------------------------------------------------------

VARIETY_RULES = {
    # Fraction of exercises that must be new versus the previous N routines. Keeps
    # programming fresh without churning the core lifts that need continuity.
    "min_new_exercise_share": 0.40,
    "compare_against_last_n_routines": 2,
    # Core compound lifts are exempt: rotating the squat every block is bad
    # programming, not creativity.
    "continuity_exempt_patterns": {"Knee Dominant", "Hip Hinge"},
}


# ---------------------------------------------------------------------------
# Resolving multiple weighted goals
# ---------------------------------------------------------------------------

@dataclass
class ResolvedPrescription:
    """A single prescription merged from the trainee's weighted goals."""

    volume_large: VolumeLandmarks
    volume_small: VolumeLandmarks
    rep_range: tuple[int, int]
    rir_range: tuple[int, int]
    rest_seconds_compound: tuple[int, int]
    rest_seconds_isolation: tuple[int, int]
    min_frequency_per_muscle: int
    min_compound_share: float
    primary_goals: list[str] = field(default_factory=list)
    all_goals: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _weights(goals: list[tuple[str, int]]) -> dict[str, float]:
    """Priority 1 counts most. Weight = 1/priority, normalized."""
    raw = {goal: 1.0 / max(priority, 1) for goal, priority in goals}
    total = sum(raw.values()) or 1.0
    return {goal: weight / total for goal, weight in raw.items()}


def resolve_prescription(
    goals: list[tuple[str, int]],
    age: int | None = None,
) -> ResolvedPrescription:
    """Merge weighted goals into one prescription, then apply age adjustment.

    `goals` is a list of (goal, priority) with priority 1 = highest. Unknown goal
    names are ignored rather than raising, so a new enum value in the database cannot
    break routine generation.
    """
    known = [(g, p) for g, p in goals if g in GOAL_PRESCRIPTIONS]
    if not known:
        known = [("general_fitness", 1)]

    weights = _weights(known)
    notes: list[str] = []

    def blend(attr: str, index: int) -> float:
        return sum(
            getattr(GOAL_PRESCRIPTIONS[g], attr)[index] * w for g, w in weights.items()
        )

    def blend_volume(attr: str) -> VolumeLandmarks:
        landmarks = [
            (getattr(GOAL_PRESCRIPTIONS[g], attr), w) for g, w in weights.items()
        ]
        return VolumeLandmarks(
            mev=sum(l.mev * w for l, w in landmarks),
            mav=sum(l.mav * w for l, w in landmarks),
            mrv=sum(l.mrv * w for l, w in landmarks),
        )

    adjustment = age_adjustment(age)
    if adjustment.note:
        notes.append(adjustment.note)

    large = blend_volume("volume_large")
    small = blend_volume("volume_small")
    large = VolumeLandmarks(large.mev, large.mav, large.mrv * adjustment.mrv_multiplier)
    small = VolumeLandmarks(small.mev, small.mav, small.mrv * adjustment.mrv_multiplier)

    goal_names = [g for g, _ in known]
    primary = [g for g, p in known if p == min(p for _, p in known)]

    # Concurrent-goal warnings the model should see and reason about.
    if "fat_loss" in goal_names and ("strength" in goal_names or "hypertrophy" in goal_names):
        notes.append(
            "Fat loss and strength/hypertrophy are pursued together: hold training "
            "intensity and volume steady and let energy balance drive fat loss. Do not "
            "reduce load or add high-rep 'toning' work."
        )
    if "endurance" in goal_names and "strength" in goal_names:
        notes.append(
            "Concurrent endurance and strength goals: separate hard conditioning from "
            "heavy lower-body sessions to limit the interference effect."
        )

    return ResolvedPrescription(
        volume_large=large,
        volume_small=small,
        # Union the rep ranges so a blended prescription permits both ends, and take
        # the tightest RIR (the most cautious proximity to failure).
        rep_range=(
            min(GOAL_PRESCRIPTIONS[g].rep_range[0] for g in goal_names),
            max(GOAL_PRESCRIPTIONS[g].rep_range[1] for g in goal_names),
        ),
        rir_range=(
            round(blend("rir_range", 0)),
            round(blend("rir_range", 1)),
        ),
        rest_seconds_compound=(
            round(blend("rest_seconds_compound", 0)),
            round(blend("rest_seconds_compound", 1)),
        ),
        rest_seconds_isolation=(
            round(blend("rest_seconds_isolation", 0)),
            round(blend("rest_seconds_isolation", 1)),
        ),
        min_frequency_per_muscle=max(
            GOAL_PRESCRIPTIONS[g].min_frequency_per_muscle for g in goal_names
        ),
        min_compound_share=max(
            GOAL_PRESCRIPTIONS[g].min_compound_share for g in goal_names
        ),
        primary_goals=primary,
        all_goals=goal_names,
        notes=notes,
    )


def progression_for(level: str) -> dict[str, object]:
    return PROGRESSION_MODELS.get(level, PROGRESSION_MODELS["novice"])
