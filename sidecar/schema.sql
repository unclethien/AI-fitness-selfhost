-- Exercise intelligence store.
--
-- Holds the full 31-attribute taxonomy from the Functional Fitness Exercise Database
-- alongside wger's 828 upstream exercises, normalized into one schema so the agent
-- queries a single pool. wger's own Exercise model has no fields for movement pattern,
-- plane of motion, posture, grip, load position, laterality or difficulty tier, which
-- is why this store exists rather than extending wger.
--
-- Cross-linked to wger by wger_exercise_id / wger_uuid, populated by the import
-- management command once the exercise exists on the wger side.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TYPE exercise_source AS ENUM ('ffed-2.9', 'wger-upstream', 'generated-variation');

CREATE TABLE exercises (
    id                    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    uuid                  UUID        NOT NULL UNIQUE,
    source                exercise_source NOT NULL,
    source_row            INTEGER,
    slug                  TEXT        NOT NULL UNIQUE,
    name                  TEXT        NOT NULL,

    -- Difficulty: 8-tier scale (Beginner .. Legendary). No wger equivalent.
    difficulty            TEXT,
    difficulty_rank       SMALLINT CHECK (difficulty_rank BETWEEN 1 AND 8),

    -- Muscles, at the source database's precision (finer than wger's 15 muscles).
    target_muscle_group   TEXT,
    prime_mover_muscle    TEXT,
    secondary_muscle      TEXT,
    tertiary_muscle       TEXT,

    -- Equipment, at the source database's precision (32 primary types vs wger's 12).
    primary_equipment     TEXT,
    primary_items         SMALLINT,
    secondary_equipment   TEXT,
    secondary_items       SMALLINT,

    -- Biomechanics. This block is the whole reason for the sidecar; none of it fits
    -- into wger's data model, and it is what makes exercise selection defensible
    -- rather than name-matching.
    posture               TEXT,
    arm_involvement       TEXT,   -- Single Arm | Double Arm | No Arms
    arm_action            TEXT,   -- Continuous | Alternating
    grip                  TEXT,
    load_position         TEXT,
    leg_action            TEXT,   -- Continuous | Alternating
    foot_elevation        TEXT,
    movement_patterns     TEXT[]  NOT NULL DEFAULT '{}',
    planes_of_motion      TEXT[]  NOT NULL DEFAULT '{}',
    body_region           TEXT,
    force_type            TEXT,
    mechanics             TEXT CHECK (mechanics IN ('Compound', 'Isolation')),
    laterality            TEXT,
    classification        TEXT,
    is_combo              BOOLEAN NOT NULL DEFAULT FALSE,

    -- Media. wger's video model takes binary uploads only, so YouTube links live here.
    video_demo_url        TEXT,
    video_explain_url     TEXT,

    -- Prose. NULL until generated; wger requires >= 40 chars to create a translation.
    description           TEXT,

    -- wger cross-link and the mapped-down taxonomy actually used on the wger side.
    wger_exercise_id      INTEGER UNIQUE,
    wger_uuid             UUID,
    wger_category         SMALLINT,
    wger_muscles          SMALLINT[] NOT NULL DEFAULT '{}',
    wger_muscles_secondary SMALLINT[] NOT NULL DEFAULT '{}',
    wger_equipment        SMALLINT[] NOT NULL DEFAULT '{}',

    qc_flags              TEXT[]  NOT NULL DEFAULT '{}',
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The agent filters on combinations of these, so they are indexed individually
-- (Postgres can combine them via bitmap AND) rather than as one composite index.
CREATE INDEX exercises_difficulty_rank_idx ON exercises (difficulty_rank);
CREATE INDEX exercises_body_region_idx     ON exercises (body_region);
CREATE INDEX exercises_mechanics_idx       ON exercises (mechanics);
CREATE INDEX exercises_laterality_idx      ON exercises (laterality);
CREATE INDEX exercises_force_type_idx      ON exercises (force_type);
CREATE INDEX exercises_primary_equipment_idx ON exercises (primary_equipment);
CREATE INDEX exercises_target_muscle_idx   ON exercises (target_muscle_group);
CREATE INDEX exercises_source_idx          ON exercises (source);
CREATE INDEX exercises_loggable_idx        ON exercises (wger_exercise_id)
    WHERE wger_exercise_id IS NOT NULL;

-- Array columns need GIN for containment/overlap queries
-- ("any exercise whose movement patterns overlap {Hip Hinge, Knee Dominant}").
CREATE INDEX exercises_movement_patterns_idx ON exercises USING GIN (movement_patterns);
CREATE INDEX exercises_planes_idx            ON exercises USING GIN (planes_of_motion);
CREATE INDEX exercises_wger_equipment_idx    ON exercises USING GIN (wger_equipment);
CREATE INDEX exercises_wger_muscles_idx      ON exercises USING GIN (wger_muscles);
CREATE INDEX exercises_qc_flags_idx          ON exercises USING GIN (qc_flags);

-- Trigram index for fuzzy name lookup ("did you mean 'Bulgarian split squat'?").
CREATE INDEX exercises_name_trgm_idx ON exercises USING GIN (name gin_trgm_ops);

CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER exercises_touch_updated_at
    BEFORE UPDATE ON exercises
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- Review gate for AI-generated exercise variations.
--
-- Recombining equipment x posture x grip x movement pattern can produce movements that
-- are nonsensical or unsafe. Nothing reaches wger (or the exercises table) until a
-- human approves it, because this stack writes into a real training log.
CREATE TYPE variation_status AS ENUM ('pending', 'approved', 'rejected');

CREATE TABLE staged_variations (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    status            variation_status NOT NULL DEFAULT 'pending',
    name              TEXT NOT NULL,
    description       TEXT NOT NULL,
    rationale         TEXT,          -- why the model thinks this variation is useful
    -- Which existing exercises it was derived from, for review context.
    derived_from      BIGINT[] NOT NULL DEFAULT '{}',
    attributes        JSONB NOT NULL, -- same attribute shape as `exercises`
    model             TEXT NOT NULL,  -- OpenRouter model id that produced it
    generation_cost_usd NUMERIC(10, 6),
    reviewer_note     TEXT,
    reviewed_at       TIMESTAMPTZ,
    -- Set once approved and promoted into `exercises`.
    promoted_exercise_id BIGINT REFERENCES exercises (id) ON DELETE SET NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX staged_variations_status_idx ON staged_variations (status, created_at DESC);

-- Chat sessions for the web UI. Kept here rather than in wger so wger's schema is
-- untouched and a reset of the AI layer cannot affect training data.
CREATE TABLE chat_sessions (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    title       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE chat_messages (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id  BIGINT NOT NULL REFERENCES chat_sessions (id) ON DELETE CASCADE,
    role        TEXT NOT NULL CHECK (role IN ('system', 'user', 'assistant', 'tool')),
    content     TEXT,
    -- Raw OpenAI-format tool_calls / tool_call_id, preserved verbatim so a
    -- conversation can be replayed to the model without lossy reconstruction.
    tool_calls  JSONB,
    tool_call_id TEXT,
    model       TEXT,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    cost_usd    NUMERIC(10, 6),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX chat_messages_session_idx ON chat_messages (session_id, id);

CREATE TRIGGER chat_sessions_touch_updated_at
    BEFORE UPDATE ON chat_sessions
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- Routines the agent has written into wger, so the UI can show provenance and the
-- agent can avoid regenerating something near-identical.
CREATE TABLE generated_routines (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    wger_routine_id   INTEGER,
    session_id        BIGINT REFERENCES chat_sessions (id) ON DELETE SET NULL,
    name              TEXT NOT NULL,
    request_summary   TEXT,
    payload           JSONB NOT NULL,  -- the full validated routine tree that was sent
    model             TEXT NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX generated_routines_created_idx ON generated_routines (created_at DESC);

-- Convenience view: exercises the agent may put in a routine. An exercise without a
-- wger id cannot be logged, so it must not be selected for a real routine.
CREATE VIEW loggable_exercises AS
SELECT *
FROM exercises
WHERE wger_exercise_id IS NOT NULL;


-- ============================================================================
-- Trainee profile
--
-- The agent's programming principles are generic; this is what makes output
-- personal. Read before every routine generation, together with recent logs.
-- ============================================================================

CREATE TYPE experience_level AS ENUM (
    'novice',        -- < 6 months consistent training; linear progression works
    'intermediate',  -- 6 months - 2 years; needs double progression / undulation
    'advanced'       -- 2+ years; needs block periodization / autoregulation
);

CREATE TABLE trainee_profile (
    id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    display_name       TEXT,

    -- Age drives recovery capacity and joint-loading caution. Stored as birth year
    -- rather than age so it does not silently go stale.
    birth_year         SMALLINT CHECK (birth_year BETWEEN 1920 AND 2020),
    -- Free text rather than an enum: used only for light programming adjustments and
    -- for load/bodyweight-ratio context. Optional; NULL is fully supported.
    gender             TEXT,
    bodyweight_kg      NUMERIC(5, 2),
    height_cm          SMALLINT,

    experience_level   experience_level NOT NULL DEFAULT 'novice',
    -- Months of consistent training, if known. More granular than the enum.
    training_age_months SMALLINT,

    -- Schedule reality. Routines that don't fit the week don't get done.
    sessions_per_week  SMALLINT NOT NULL DEFAULT 3
        CHECK (sessions_per_week BETWEEN 1 AND 7),
    minutes_per_session SMALLINT NOT NULL DEFAULT 60
        CHECK (minutes_per_session BETWEEN 15 AND 240),

    -- Equipment the trainee actually owns, matching exercises.primary_equipment
    -- values. Empty array is treated as "unknown / no restriction" by the filter,
    -- which is deliberately permissive so an unfilled profile still works.
    available_equipment TEXT[] NOT NULL DEFAULT '{}',

    -- Exercises or styles the trainee simply won't do. Distinct from
    -- contraindications: a preference, not a safety boundary.
    dislikes           TEXT[] NOT NULL DEFAULT '{}',

    notes              TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TRIGGER trainee_profile_touch_updated_at
    BEFORE UPDATE ON trainee_profile
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- Goals are weighted, not singular. "General fitness + fat loss + strength" is the
-- common real case, and those goals imply partly conflicting programming (a deficit
-- blunts strength adaptation), so the generator needs to know what to prioritize.
CREATE TYPE training_goal AS ENUM (
    'general_fitness',
    'fat_loss',
    'strength',
    'hypertrophy',
    'endurance',
    'mobility',
    'skill'
);

CREATE TABLE trainee_goals (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    profile_id  BIGINT NOT NULL REFERENCES trainee_profile (id) ON DELETE CASCADE,
    goal        training_goal NOT NULL,
    -- 1 = primary driver of programming decisions. Ties are allowed.
    priority    SMALLINT NOT NULL DEFAULT 1 CHECK (priority BETWEEN 1 AND 5),
    UNIQUE (profile_id, goal)
);

-- Contraindications are a hard safety boundary, so they are modelled as
-- machine-actionable filters rather than free text the model might overlook.
--
-- Scope note: these are TRAINEE-DECLARED restrictions. Nothing here diagnoses,
-- assesses, or substitutes for a clinician.
CREATE TYPE contraindication_kind AS ENUM (
    'exercise',          -- one specific exercise, by sidecar id
    'movement_pattern',  -- e.g. 'Vertical Push' after a shoulder impingement
    'equipment',         -- e.g. 'Barbell'
    'posture',           -- e.g. 'Hanging'
    'body_region',       -- e.g. 'Lower Body'
    'plane_of_motion',
    'classification',    -- e.g. 'Plyometric' when impact is contraindicated
    'max_difficulty'     -- value is a difficulty_rank ceiling, 1-8
);

CREATE TABLE trainee_contraindications (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    profile_id  BIGINT NOT NULL REFERENCES trainee_profile (id) ON DELETE CASCADE,
    kind        contraindication_kind NOT NULL,
    -- Interpreted according to `kind`. Text for all kinds, including numeric
    -- ceilings, so one column serves every case.
    value       TEXT NOT NULL,
    -- Why, in the trainee's words. Surfaced to the model as context and shown back
    -- in the UI so an outdated restriction is easy to spot and remove.
    reason      TEXT,
    -- Set when the restriction is temporary (healing injury).
    expires_on  DATE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (profile_id, kind, value)
);

CREATE INDEX trainee_contraindications_profile_idx
    ON trainee_contraindications (profile_id)
    WHERE expires_on IS NULL OR expires_on >= CURRENT_DATE;

-- Known working loads, so prescribed weights are real numbers rather than "moderate".
CREATE TABLE trainee_benchmarks (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    profile_id   BIGINT NOT NULL REFERENCES trainee_profile (id) ON DELETE CASCADE,
    exercise_id  BIGINT REFERENCES exercises (id) ON DELETE SET NULL,
    -- Free-text fallback for a lift that isn't in the pool.
    label        TEXT,
    weight_kg    NUMERIC(6, 2),
    reps         SMALLINT,
    recorded_on  DATE NOT NULL DEFAULT CURRENT_DATE,
    CHECK (exercise_id IS NOT NULL OR label IS NOT NULL)
);

CREATE INDEX trainee_benchmarks_profile_idx
    ON trainee_benchmarks (profile_id, recorded_on DESC);

-- Outcome of the generate -> validate -> critic -> revise loop, kept for every
-- attempt so programming quality is auditable over time rather than anecdotal.
CREATE TABLE routine_reviews (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    generated_routine_id BIGINT REFERENCES generated_routines (id) ON DELETE CASCADE,
    iteration           SMALLINT NOT NULL DEFAULT 1,
    -- Deterministic validator output: [] means every principle check passed.
    violations          JSONB NOT NULL DEFAULT '[]',
    -- Free-text critique from the reviewing-coach model pass.
    critic_model        TEXT,
    critic_verdict      TEXT CHECK (critic_verdict IN ('approve', 'revise')),
    critic_notes        TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX routine_reviews_routine_idx ON routine_reviews (generated_routine_id, iteration);
