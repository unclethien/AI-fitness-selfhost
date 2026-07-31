# Self-hosted wger + AI exercise agent

**Status:** All phases code complete. Phases 1-6b written; the TrueNAS stack itself
now runs (steps 0-6 of the runbook executed 2026-07-30). Phase 2 remains **unverified against
a live database** — Docker is not installed on this machine, so nothing has actually been
run against Postgres. `sidecar/schema.sql` was validated against the real Postgres 17
grammar via `pglast` (30 statements parse), which catches syntax but not semantics.
**Created:** 2026-07-30

## Outcome

A self-hosted fitness stack where an AI agent generates workout routines — and new
exercise variations — from a combined exercise pool of wger's 828 upstream exercises
plus the 3,242-exercise Functional Fitness Exercise Database (v2.9), and writes real
routines into wger via its REST API.

## Accepted decisions

| Decision | Choice | Rationale |
|---|---|---|
| Agent scope | Workout routines + new exercise variations + recent-log reading | User-selected. Full adaptive coaching still out of scope; the agent reads recent logs as *input* to each routine. |
| Coaching quality | Option C — principles config + deterministic validator + critic pass | User-selected. Expert-grade becomes verifiable rather than plausible. |
| Trainee profile | First-class feature: goals (weighted), age, gender, injuries/avoid-movements, equipment, schedule | User-requested form. Read before every generation. |
| Interface | Self-hosted web chat UI | Usable from phone; own service beside wger. |
| LLM provider | **OmniRoute self-hosted on TrueNAS**, behind a provider-neutral client | User's choice. Any OpenAI-compatible endpoint works via `LLM_BASE_URL` — OmniRoute, OpenRouter, Ollama, vLLM — so this is a config change, not a rewrite. |
| Custom DB integration | Hybrid — sidecar + mapped import into vanilla wger | Keeps wger upgradeable while preserving the full 31-attribute taxonomy for AI retrieval. |
| Language | Python | wger is Django; ETL is data-heavy; OpenAI SDK is first-class. |
| Deployment | **TrueNAS SCALE custom app**, single Compose project via `include:` | User hosts on TrueNAS. One app avoids cross-project networking; `include:` keeps wger's compose untouched. Bind mounts to ZFS datasets put training data under snapshots. |

## Non-goals

- Adaptive/progressive coaching from workout logs (dropped by user in second pass).
- AI-written exercise descriptions for the existing 3,242 (deterministic descriptions
  generated from attributes instead — see Phase 3).
- Forking wger. Its models stay untouched so upstream upgrades remain safe.

## Constraints discovered from the source systems

Verified against `wger.de/api/v2/schema` (v2.7.0a1) and the spreadsheet itself on 2026-07-30:

- `exercisecategory`, `muscle`, and `equipment` are **GET-only** over REST. The 26 new
  equipment rows must be created through Django (management command), not the API.
- `POST /api/v2/exercise/` treats `uuid` as **read-only**, so idempotent re-import
  requires ORM access. Import therefore runs as a Django management command inside the
  wger container, not as an API client.
- `exercise-translation.description_source` has a **40-character minimum**. Every
  imported exercise needs prose; the custom DB has none.
- `POST /api/v2/video/` takes a **binary file upload**, not a URL. The 2,963 YouTube
  links cannot go into wger's video model — they live in the sidecar and in the
  rendered description HTML.
- Routines are fully writable: `routine → day → slot → slot_entry → {weight,
  repetitions,sets,rir,rest}-config`. Multiple entries in one slot = superset.
  Routine `name` is capped at 25 characters. Day types include `amrap`, `hiit`,
  `tabata`, `emom`, `rft`, `afap`.
- ~12 muscles in the custom DB have no wger equivalent and map to null. This is
  expected, not a mapping gap; the sidecar retains the precise muscle.

## Architecture

```
┌─────────────────┐   REST (routines, logs)   ┌──────────────────────┐
│  wger (vanilla) │◀──────────────────────────│  agent service       │
│  Django + PG    │                            │  OpenRouter client   │
│  logging, UI,   │                            │  tool loop           │
│  mobile apps    │                            └──────┬───────────────┘
└────────┬────────┘                                   │ SQL
         │ ORM (import command)                        ▼
         │                              ┌──────────────────────────────┐
         └──────────────────────────────│  exercise intelligence store │
                                        │  Postgres, 31 attributes,    │
                                        │  4,070 exercises, cross-     │
                                        │  linked by wger id/uuid      │
                                        └──────────────────────────────┘
                                                       ▲
                                                       │
                                        ┌──────────────────────────────┐
                                        │  web chat UI (SSE streaming) │
                                        └──────────────────────────────┘
```

wger stays unmodified. The sidecar is the AI's retrieval surface. The agent reads the
sidecar for exercise selection and writes routines through wger's REST API.

## LLM gateway — OmniRoute (self-hosted)

`agent/llm.py` is provider-neutral: one OpenAI-compatible client, `LLM_BASE_URL` selects
the gateway. Three OmniRoute behaviours needed explicit defences, because each fails
*silently* and would read as a model-quality problem rather than plumbing:

| Risk | Why it matters here | Defence |
|---|---|---|
| Tool calling is tiered `native` / `emulated` / `none`, and `none` **silently drops the tools array** | The agent would appear to work and never search the exercise database | `preflight()` forces a tool call and checks a structured `tool_call` came back. `GET /capabilities` surfaces it; `emulated` gets a brittleness warning |
| `response_format: json_schema` is undocumented | The routine plan schema depends on it | `structured()` degrades `json_schema` → forced tool call → prompt + parse + repair, and reports which strategy ran |
| Token compression targets "bloated JSON and repeated context" | That is *exactly* the shape of the candidate exercise list and nested routine plan; a rewritten exercise id is a corrupted routine | `x-omniroute-compression: off` sent by default on every request; opt back in per client |

`agent/test_llm.py` — 33 tests, all passing, each simulating a specific badly-behaved
gateway: dropped tools, emulated tools, rejected `json_schema`, markdown-fenced JSON,
unparseable-then-repaired JSON, a raising tool, malformed tool arguments, runaway loop.

**Self-hosting the gateway does not make inference local.** Unless OmniRoute is pointed
at a local backend (Ollama / LM Studio / vLLM), prompts — including the trainee profile
and recent logs — still leave the box. The 5060 Ti closes that loop if desired, through
the same gateway and with no code change.

## Model routing (ids verified against OpenRouter 2026-07-30; confirm against your own gateway)

| Job | Model | Price /Mtok | Why |
|---|---|---|---|
| Routine generation | `anthropic/claude-sonnet-5` | $2 / $10 | Default. Handles the 4-level nested routine JSON. |
| Hard routine escalation | `anthropic/claude-opus-5` | $5 / $25 | On validation failure or explicit user request. |
| Exercise variation generation | `anthropic/claude-sonnet-5` | $2 / $10 | Safety-adjacent output; needs good judgment. |
| Cheap/offline experiments | `qwen/qwen3-30b-a3b-instruct-2507` | $0.05 / $0.19 | Also the size class that fits the user's 16GB 5060 Ti locally. |

Configured per-job in `agent/config.py`, overridable by env var. OpenRouter's
`extra_body={"models": [...]}` provides automatic fallback; `:batch` variants give 50%
off for any future bulk job.

## Phases

### Phase 1 — Extract and normalize the custom database ✅

`etl/extract_custom_db.py` + `etl/mappings/wger_mappings.json`.

Asserts the spreadsheet layout (header row 16, columns B..AF) rather than assuming it,
so a future v3.0 fails loudly. Resolves the YouTube URLs from cell hyperlinks, collapses
the numbered multi-value columns, validates enums, and maps a subset onto wger's taxonomy.

Results: **3,242 exercises**, 2,013 demo videos, 950 explainers, 2,151 records clean,
1,091 flagged. Deterministic UUIDv5 per exercise from a project-local namespace, so
re-import updates rather than duplicates and can never collide with an upstream
wger.de UUID.

Data-quality findings (full detail in `build/qc_report.md`):

| Issue | Count | Handling |
|---|---|---|
| `Primary Exercise Classification` = `Unsorted*` | 602 | Nulled + flagged |
| `Force Type` = `Unsorted*` | 364 | Nulled + flagged |
| `Load Position` = `Order` (not a load position) | 192 | Retained + flagged for review |
| No usable movement pattern | 62 | Flagged |
| Name has stray leading/trailing whitespace | 44 | Trimmed |
| `Mechanics` blank | 13 | Flagged |
| Blank plane of motion | 4 | Flagged |
| `Mechanics` = `Pull` (invalid enum) | 2 | Nulled + flagged |
| Duplicate exercise names | 2 pairs | Slug disambiguated + flagged |

### Phase 2 — Stack + sidecar store (code complete, unrun)

- `setup.sh` clones `wger-project/docker` into `vendor/wger` **unmodified**. Upstream's
  compose file uses `include:` with its own service files, so vendoring a rewritten copy
  would guarantee drift. Ours attaches to the external `wger_network` instead.
- `docker-compose.yml`: sidecar Postgres + agent service, joined to wger's network.
- `sidecar/schema.sql`: full 31-attribute taxonomy, plus tables for staged variations,
  chat sessions and generated routines. GIN indexes on every array column (movement
  patterns, planes, wger muscle/equipment IDs, QC flags) so multi-attribute filtering
  is a bitmap AND rather than a scan; trigram index on name for fuzzy lookup.
- `sidecar/describe.py`: deterministic Markdown descriptions from attributes. Verified
  over all 3,242 records — 363–775 chars, zero below wger's 40-char floor.
- `sidecar/load.py`: idempotent upsert keyed on UUID for both sources. Deliberately does
  not overwrite `wger_exercise_id`, so a re-run cannot break the cross-link.

**Blocker:** Docker is not installed on this machine (`docker` is not on PATH, and there
is no local Postgres either). None of Phase 2 has been executed. `setup.sh` fails fast
with install instructions rather than producing a confusing downstream error.

### Phase 3 — Import into wger ✅ (code complete, tested)

`wger_import/import_exercises.py` + `agent/import_api.py` + `wger_import/README.md`.

Field names verified against wger's actual Django models on GitHub, not guessed:
`Exercise` (base.py), `Translation` (translation.py), `Equipment`, `Muscle`,
`ExerciseCategory`, `Language.short_name`, `wger.utils.constants.ENGLISH_SHORT_NAME`.

Key confirmations from reading the source:

- `Translation.save()` renders `description_source` → the read-only `description` field
  via `render_markdown`. Writing Markdown into `description_source` is therefore exactly
  right, and matches how `sidecar/describe.py` already generates text.
- `Translation` has a unique constraint on `(exercise, language)`, so translations are
  `update_or_create`d on that pair.
- `Translation.description` is capped at 2000 chars and holds *rendered* HTML, so the
  API trims `description_source` to 900 chars on a paragraph boundary to leave headroom.
- `Exercise.uuid` is `editable=False`, which blocks forms but not the ORM — the whole
  reason this is a script rather than a REST client.

**How it avoids touching wger** — four deliberate choices:

1. Piped into `manage.py shell`, where Django is already configured. No app registered,
   no `INSTALLED_APPS` edit, no settings module to locate, nothing mounted.
2. Not the REST API: `POST /api/v2/exercise/` treats `uuid` as read-only, and the import
   depends on a deterministic project-local UUIDv5 so re-running updates in place.
3. Talks HTTP to the agent rather than SQL to the sidecar, so it needs only the stdlib —
   independent of whatever driver the wger image ships.
4. Taxonomy exchanged **by name**, since wger's fixture ids are not guaranteed identical
   across installations. Missing equipment is created; a missing category is a loud error.

Safety properties: one transaction per exercise (a bad row cannot half-build an exercise
or abort the run); unknown muscle names degrade to a warning rather than aborting, since
~12 muscles deliberately map to nothing; `DRY_RUN` and `IMPORT_LIMIT` for staged rollout.

`wger_import/test_import.py` — 36 checks, all passing, across the API and the script
logic: name-based taxonomy resolution, description trimming on a paragraph boundary,
idempotent re-import, unknown-muscle degradation, unknown-category hard failure, dry run,
import limit, malformed link payload, unreachable agent.

**Bug found by testing:** the paging loop deliberately does not advance the offset when
importing only pending exercises (processed rows leave the result set) — but if the
link-back ever failed, the same page would be served forever and the script would spin
indefinitely. Added a no-progress guard that tracks handled uuids and stops with a
diagnostic pointing at the link-back.

What the import does, in order:

1. Creates the 26 equipment types the custom database uses and wger lacks (idempotent
   by name; equipment rows are name-only, so this cannot disturb wger's own exercises).
2. `update_or_create`s each exercise on its project-local UUIDv5, with the mapped
   category, muscles and equipment.
3. Creates one English translation per exercise carrying the **deterministically
   generated** description — built from the exercise's own attributes plus its YouTube
   links, factual by construction, no LLM in this path.
4. Writes each new wger exercise id back into the sidecar, completing the cross-link so
   the agent knows which exercises are loggable.

Operational note: never run wger's `sync-exercises` with a delete flag. Imported
exercises carry project-local UUIDs absent from upstream's deletion log. Ordinary syncing
is fine, and re-running the import restores anything lost regardless.

### Phase 4a — Coaching intelligence ✅ (code complete, tested)

The part that makes output expert-grade rather than expert-sounding. Deliberately
*not* a large system prompt: a prompt can be ignored, a validator cannot.

**`coaching/principles.py`** — programming knowledge as tunable configuration:

- Volume landmarks (MEV / MAV / MRV) per goal, split by muscle size class
- Rep ranges, RIR bands, rest periods, minimum frequency, compound share per goal
- `MUSCLE_TO_GROUP`: maps the database's 40+ specific muscles onto the 16 coarse
  target groups the landmarks are keyed on, so indirect volume lands in the right
  bucket (a bench press still credits triceps)
- Progression model by experience level — linear / double progression /
  autoregulated-RIR, with concrete increments and deload cadence
- Age adjustment to the volume ceiling (conservative, easily overridden)
- Movement-pattern coverage, plane requirements, push/pull and upper/lower balance
- `CONDITIONING_RULES` for concurrent-training interference
- Session-length feasibility model
- Variety rules, with core compounds exempt (rotating the squat every block is bad
  programming, not creativity)
- `resolve_prescription()` merges *weighted* goals into one prescription and emits
  conflict notes — e.g. fat loss + strength produces an explicit instruction to hold
  intensity and let energy balance drive fat loss

Sources cited inline: NSCA *Essentials* 4th ed., ACSM progression position stand,
Schoenfeld et al. 2016 (frequency) and 2017 (volume dose-response), Israetel/RP
volume landmarks, Hickson 1980 + concurrent-training literature, Helms et al. 2016
(RIR autoregulation).

**`coaching/routine_schema.py`** — one JSON Schema serving three jobs: constrains the
model's output via OpenRouter structured outputs, is the validator's input contract,
and is what gets translated into wger API calls. Field length limits mirror wger's
serializers exactly (routine name 25, day name 20, slot comment 200, entry comment
100) so the model cannot produce a plan that only fails at the final POST.

**`coaching/validate.py`** — 11 check families returning structured violations at
error / warning / info severity, renderable as a revision prompt:

1. Referential integrity — unknown ids, non-loggable exercises, id/name mismatch
2. Contraindications — 8 kinds, all `error` severity (the safety boundary)
3. Equipment availability
4. Weekly volume vs MEV/MRV per muscle group
5. Frequency per muscle group
6. Pattern coverage, push/pull and upper/lower balance, tri-planar coverage
7. Exercise order — compound-before-isolation, high-fatigue-first
8. Compound share, rep/RIR/rest adherence
9. Schedule feasibility — session count and estimated duration
10. Concurrent-training interference
11. Variety vs recent routines and recent logs; progression/experience match

**`coaching/test_validate.py`** — 33 tests, all passing. Each asserts a specific
broken routine produces the specific violation code it should, plus a baseline
"reasonable routine" test proving the validator isn't merely noisy.

Bugs found and fixed during testing: a fixture sentinel bug that made the
"not imported into wger" case silently loggable; volume double-reporting at two
muscle granularities; float-boundary messages ("17.0 exceeds the maximum of 17");
and a grammar bug in the pattern-coverage message.

Schema additions (`trainee_profile`, `trainee_goals`, `trainee_contraindications`,
`trainee_benchmarks`, `routine_reviews`) bring the sidecar to 42 statements, all
parsing against the Postgres 17 grammar.

### Phase 4b — Agent service ✅ (code complete, tested)

`agent/wger_client.py`, `agent/exercise_search.py`, `agent/tools.py`,
`agent/generate.py`, plus `POST /api/routine/generate`.

**Six tools**, with descriptions that are prescriptive about *when* to call each — the
profile and recent logs are described as mandatory-first, since without them the output
is generic by construction.

Deliberate design point: **the model cannot write to wger.** There is no
`create_routine` tool. The model calls `submit_routine_plan`, which only queues the plan
for review; `generate.py` validates, critiques and then writes. Giving the model a write
tool would defeat the entire Option C design.

**Contraindications are applied in SQL, not left to the model.** `search_exercises`
filters them out before results are returned, so a contraindicated exercise never appears
in a candidate list. Relying on a model to remember an injury across a long tool-calling
session is exactly what fails quietly.

**The pipeline** (`generate.py`): draft with tools → deterministic validation → critic
pass → revise with violations + critique as explicit instructions → write. Escalates to
the stronger model after two failed reviews rather than looping on the same one. Both the
validator output and the critic verdict are persisted per iteration in `routine_reviews`.

Graceful degradation, each tested: a failed critic does not block a routine that passed
every deterministic check; an unreachable wger degrades log reading rather than refusing;
bookkeeping failures never lose a routine the user already has.

**The routine writer** is the riskiest piece — a 4-day routine is ~100 requests across
`routine → day → slot → slot_entry → configs`, with no bulk endpoint. So it rolls back:
any failure deletes the partially-created routine, and if the rollback itself fails the
error says explicitly which routine id needs manual deletion. wger's short string limits
(routine name 25, day name 20) are checked before the first write.

Known uncertainty: the config write path sets base values at iteration 1 with
`operation: "r"` and progression at a later iteration with `repeat: true`. wger's schema
documents the fields but not their interaction, so this is the part most likely to need
adjustment against a live server. It is isolated in `_write_entry` for that reason.

`agent/test_generate.py` — 47 checks, all passing, covering the failure paths that matter:
validator blocks a bad plan then accepts the revision; critic vetoes a rule-passing plan;
repeated failure escalates then gives up rather than looping; an exercise never imported
into wger is refused and nothing is written; a wger write failure surfaces cleanly with
the plan retained; dry run writes nothing; a model that never submits is reported clearly.

### Phase 4b — original sketch

FastAPI + OpenAI SDK pointed at OpenRouter. Tool surface:

| Tool | Purpose |
|---|---|
| `get_trainee_profile` | Goals (weighted), age, gender, experience, schedule, equipment, contraindications, benchmarks. **Called first, every time.** |
| `get_recent_logs` | Read-only wger workout logs so routines account for what was actually trained |
| `search_exercises` | Filter the 4,070-exercise pool by equipment, difficulty range, movement pattern, plane, body region, laterality, posture |
| `get_exercise_detail` | Full attribute set + video links for specific exercises |
| `create_routine` | Write `routine → day → slot → slot_entry → configs` through wger's REST API |
| `propose_exercise_variation` | Write a candidate variation to the **staging table** — never directly into wger |

**The generation loop (Option C):**

```
get_trainee_profile  →  get_recent_logs  →  search_exercises
        ↓
  model drafts a routine plan (structured output, ROUTINE_PLAN_SCHEMA)
        ↓
  deterministic validator  ──errors?──▶ as_revision_prompt() ──▶ model revises
        ↓ clean                                                    (max 3 rounds,
  reviewing-coach critic pass                                       then escalate
  ("review another coach's program for this client")                to Opus 5)
        ↓ approve
  validate against wger's OpenAPI schema
        ↓
  POST to wger  +  record in generated_routines / routine_reviews
```

Both the validator output and the critic verdict are persisted per iteration in
`routine_reviews`, so programming quality is auditable over time rather than anecdotal.

Cost estimate at ~40 routines/month with the critic pass: roughly $30–45/month.

### Phase 5 — Variation review gate ✅ (code complete, tested)

`agent/variations.py`, `agent/templates/variations.html`, `/variations` UI,
`GET /api/variations`.

Two *different* dangers here, handled separately — the second is the one that is easy to
miss:

**1. Unsafe or silly movements.** Solved by the review gate. A proposal sits in
`staged_variations` as `pending` until approved through the UI. The page leads with an
explicit instruction to read the movement and reject anything you cannot picture
performing, because a queue invites click-through.

**2. Poisoned taxonomy.** A model that invents `"Hip Thrusty Pattern"` or
`"Diagonal Plane"` would insert an exercise whose attributes match *no* search filter and
*no* programming rule. It would quietly never be selected, or worse, silently skip the
validator's pattern and plane checks. So every attribute value is validated against the
vocabulary that actually exists in the database **before staging** — an unusable proposal
is refused with reasons and suggestions (`did you mean 'Hip Hinge'?`) rather than shown to
the reviewer as though it were fine.

Required vs optional is deliberate: a missing/invalid required attribute (target muscle
group, prime mover, equipment, body region, mechanics, laterality, ≥1 movement pattern,
≥1 plane) blocks staging. An unrecognized *optional* value (grip, difficulty) is dropped
with a warning, because an unusual-but-valid combination is what a creative variation
looks like. `target_muscle_group` is required specifically because without it there is no
wger category and the exercise could never be created in the training app.

**Approval is a two-step promotion, not one.** Approving creates a real `exercises` row
(source `generated-variation`, deterministic UUIDv5 in the project-local namespace so
re-approval cannot duplicate) — but with **no wger id, so it is not loggable** until the
wger import runs. The import query was widened to `source IN ('ffed-2.9',
'generated-variation')` so the same idempotent path handles both.

**Provenance is permanent.** The promoted description carries
`_AI-generated exercise variation (<model>), reviewed and approved on <date>._`, so anyone
looking at that exercise in wger months later can tell what it is. The staging row also
records which model produced it.

`agent/test_variations.py` — 48 checks, all passing: invented patterns/planes/mechanics/
laterality/equipment rejected with suggestions; stub descriptions rejected; optional
values degrading to warnings; nothing promoted on reject; approve→promote linkage;
approved-but-not-loggable state; deterministic UUID; double-approve and double-reject
failing cleanly; and the tool refusing to queue an invalid proposal.

**Two regressions this phase caused and fixed:** `str | None` in a FastAPI route
signature broke on Python 3.9 (Pydantic evaluates route annotations at runtime, so
`from __future__ import annotations` does not help — switched to `Optional[str]`); and
`test_generate`'s fake database did not answer the vocabulary queries the variation tool
now issues.

### Phase 5 — original sketch

Generated variations land in `staged_variations` with status `pending`. The web UI
exposes approve/reject. Only approved variations are written into wger and the sidecar.
This is deliberate: recombining equipment × posture × grip × movement pattern can
produce movements that are nonsensical or unsafe, and this stack writes into a real
training log.

### Phase 6a — Trainee profile form ✅ (code complete, tested)

`agent/main.py` (FastAPI), `agent/profile_repo.py`, `agent/templates/profile.html`,
`agent/static/{app.css,profile.js}`. Server-rendered, no build step.

Collects: weighted goals (7 options × priority 1–5), birth year, gender, bodyweight,
height, experience level, training age, sessions/week, minutes/session, available
equipment, injuries/movements to avoid, dislikes, free-text notes.

Design points worth keeping:

- **Dropdown vocabulary is queried from the exercise database**, not hardcoded, so the
  form cannot drift from the data and produce a restriction matching nothing.
- **Contraindications are machine-actionable**, 8 kinds (movement pattern, exercise,
  equipment, posture, body region, plane, classification, max difficulty) with optional
  reason and expiry. An expired restriction stops constraining programming automatically.
- **Advisory completeness check** — an incomplete profile still generates routines, the
  page just names the fields that would make them less generic.
- Works with JavaScript disabled; JS only adds/removes restriction rows.
- Explicit scope note in the UI: records trainee-declared restrictions, does not assess
  or diagnose.

`agent/test_profile_form.py` — 33 checks against a fake sidecar, all passing, including
the real handoff: saved profile → `resolve_prescription()` → fat-loss/strength conflict
note. Bugs found and fixed: `JSONResponse` has no `default=` kwarg so `/api/profile`
500'd on any date; the "+ Add restriction" button produced an empty dropdown on a fresh
profile because it cloned options from a row that didn't exist; and date normalization
was happening in the template instead of the repository, so it broke on whichever of
`date`/`str` it didn't expect.

### Phase 6b — Web chat UI ✅ (code complete, tested)

Server-rendered chat at `/chat`, no build step. 46 tests in `agent/test_chat.py`;
257 across the whole suite.

- `agent/chat_repo.py` — sessions and messages in OpenAI wire format, `tool_calls` and
  `tool_call_id` preserved verbatim so a conversation replays exactly as it happened.
- `agent/chat.py` — the conversational loop.
- `agent/templates/chat.html`, `agent/static/chat.js`, chat styles in `app.css`.
- Routes in `main.py`; `/` now lands on `/chat` rather than `/profile`.

**The design decision.** The chat does not reimplement routine generation. It gets the
generator's read tools plus one extra tool, `generate_routine`, which invokes the tested
pipeline in `generate.py` and streams its progress into the transcript.
`submit_routine_plan` is deliberately withheld — exposing both would give the model two
ways to produce a routine, only one of them validated. This makes "is this actually a
request for a program?" a judgement the model makes in context: a UI mode switch would
force that decision before the question is asked, and a classifier call would add a round
trip and a failure mode to every message.

**Two deviations from the sketch, both deliberate:**

1. **NDJSON over a streamed POST, not SSE.** `EventSource` is GET-only, so SSE would mean
   stashing the message server-side and fetching it back. `fetch` with a streamed body
   reads it directly. `X-Accel-Buffering: no` is set so it still works if ever proxied
   through nginx.
2. **Event streaming, not token streaming.** Progress arrives live — each search, each
   validation result, the critic verdict — but the prose reply arrives whole.
   `llm.run_tools` is not streaming-capable, and adding that to the module every other
   surface depends on is a real risk for a perceived-latency gain. The part that actually
   takes tens of seconds is the pipeline, and that *is* streamed. Worth revisiting once
   the stack has run against a real model.

**Known gap:** the transcript renders as plain text with `white-space: pre-wrap`, and the
system prompt tells the model not to emit markdown. If it emits it anyway, you see the
asterisks. A renderer is a later call, not a silent `innerHTML`.

## Open questions

**Resolved:** TrueNAS SCALE confirmed. `Load Position = Order` confirmed legitimate
(clubbell "order position", from the drill command "order arms") — no longer flagged,
which moved clean records from 2,151 to 2,328. Equipment, injuries and experience level
are now collected through the profile form rather than by asking.

- **Verify the OmniRoute Docker image name.** `deploy/truenas/compose.yaml` uses
  `docker.io/diegosouzapw/omniroute:latest`, inferred from the GitHub org path rather
  than read off a registry listing. If the project only ships via npm/Electron, run it
  separately and point `LLM_BASE_URL` at `http://<truenas-ip>:20128/v1`.
- **Confirm model ids against your own gateway** once it is up: `GET /capabilities` on
  the agent lists advertised ids and probes each configured model. OpenRouter's
  `provider/model` naming may not carry over.
- Do you want inference local (Ollama/vLLM on the 5060 Ti behind OmniRoute), or is a
  cloud upstream fine? Only affects whether profile data leaves the box.

## TrueNAS deployment

`deploy/truenas/compose.yaml` — paste into Apps → Custom App → Install via YAML.
Verified against TrueNAS docs (updated April 2026): `name:`, `services:` and `include:`
are the allowed top-level keys, host-path bind mounts to datasets are the documented
production pattern, and cross-app `external: true` networks work.

Findings that shaped the file:

- **TrueNAS SCALE's web UI owns ports 80 and 443.** wger's nginx defaults to 80, so it
  is remapped to 8080. Moving the NAS UI instead risks locking you out of the box.
- Compose **appends** to a list when merging an override, so the port change needs the
  `!override` tag or `80:80` survives and the app won't start. Needs Compose ≥ 2.24;
  any SCALE 24.10+ is well past that.
- Bind mounts point at ZFS datasets, so wger's Postgres, its media, and the sidecar
  database are covered by snapshots and replication rather than opaque Docker volumes.
- Ownership matters: wger images run as UID/GID 1000, Postgres as 999. Wrong ownership
  is the most common cause of a stack that starts and then dies.
- `SITE_URL` and `CSRF_TRUSTED_ORIGINS` must match the address you actually browse to,
  or every form submission fails with a CSRF error.

**Unverified.** None of this has been executed — there is no Docker on the development
machine, so the compose file is written from documentation rather than from a working run.

## Trainee profile — captured so far

| Field | Value |
|---|---|
| Goals | general fitness, fat loss, strength (stored weighted; priorities TBD) |
| Recent-log reading | In scope |
| Age / gender / injuries / equipment | To be collected via the profile form |

The Phase 6 profile form collects: goals with priority, birth year, gender,
bodyweight/height, experience level, sessions per week, minutes per session, available
equipment, movements or exercises to avoid (with reason and optional expiry), dislikes,
and known lift benchmarks.
- The 192 `Load Position = Order` rows: leave flagged-but-retained, or null them? They
  cluster almost entirely on clubbell exercises, several of which have "Order" in the
  exercise name itself — so it may be a real (if oddly named) clubbell position rather
  than a paste artifact. Retained pending your call.
