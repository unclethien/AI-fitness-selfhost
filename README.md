# AI fitness self-host

Self-hosted [wger](https://github.com/wger-project/wger) plus an AI agent that generates
workout routines — and new exercise variations — from a combined pool of wger's 828
upstream exercises and the 3,242-exercise Functional Fitness Exercise Database (v2.9).

## How it fits together

```
┌─────────────────┐   REST (routines, logs)   ┌──────────────────────┐
│  wger (vanilla) │◀──────────────────────────│  agent service       │
│  Django + PG    │                            │  OpenRouter client   │
│  logging, UI,   │                            │  + tool loop         │
│  mobile apps    │                            └──────┬───────────────┘
└────────┬────────┘                                   │ SQL
         │ ORM (import command)                        ▼
         │                              ┌──────────────────────────────┐
         └──────────────────────────────│  exercise intelligence store │
                                        │  Postgres — 31 attributes    │
                                        │  per exercise, cross-linked  │
                                        │  to wger by id/uuid          │
                                        └──────────────────────────────┘
```

**wger is never modified.** It runs from an unmodified clone of
`wger-project/docker` in `vendor/wger`, so upstream upgrades are a plain `git pull`.
Everything in this repo attaches to it from outside.

**Why a sidecar store?** wger's `Exercise` model has no fields for movement pattern,
plane of motion, posture, grip, load position, laterality or difficulty tier — exactly
the attributes that make exercise selection defensible rather than name-matching. Rather
than fork a fast-moving 2.7-alpha Django app and own its migrations forever, the full
taxonomy lives in a sidecar the agent queries, while a mapped subset is imported into
wger so every exercise is still loggable in the web and mobile apps.

## Requirements

- **Docker** with Compose v2 (`brew install --cask orbstack`, or Docker Desktop, or colima)
- Python 3.10+ (for the host-side ETL)
- An [OpenRouter](https://openrouter.ai/keys) API key
- `Functional+Fitness+Exercise+Database+(version+2.9).xlsx` in the repo root

## Setup

```sh
./setup.sh
```

That clones the wger stack, generates a sidecar password, replaces wger's placeholder
`SECRET_KEY`, and builds a `.venv` for the ETL. It then prints the remaining manual
steps — filling in `.env`, starting the containers, creating your wger API token, and
running the extraction.

## Layout

| Path | What it is |
|---|---|
| `etl/extract_custom_db.py` | Reads the spreadsheet, normalizes 31 columns, resolves YouTube hyperlinks, maps onto wger's taxonomy, emits a QC report |
| `etl/mappings/wger_mappings.json` | The custom-taxonomy → wger-taxonomy mapping, kept as data so it is auditable |
| `sidecar/schema.sql` | Exercise intelligence store: exercises, staged variations, chat, generated routines |
| `sidecar/describe.py` | Deterministic Markdown descriptions built from attributes (no LLM) |
| `sidecar/load.py` | Idempotent upsert of both exercise sources into the sidecar |
| `plans/` | Design decisions, phase status, and open questions |
| `build/` | ETL output (gitignored; regenerate any time) |

## Extraction results

3,242 exercises, 2,013 demonstration videos, 950 explainer videos. 2,151 records clean;
1,091 carry at least one QC flag. Full breakdown in `build/qc_report.md` after running
the ETL — the headline items are 602 exercises with an `Unsorted*` classification, 364
with an `Unsorted*` force type, and 192 whose "Load Position" is the literal value
`Order`.

Descriptions are generated deterministically from each exercise's own attributes rather
than by an LLM: the source database has no prose at all, and wger requires at least 40
characters to create a translation. Output runs 363–775 characters and is factual by
construction. Individual exercises can be upgraded to AI-written coaching cues later.

## Model routing

OpenRouter model IDs and prices verified live on 2026-07-30:

| Job | Model | $/Mtok in / out |
|---|---|---|
| Routine generation | `anthropic/claude-sonnet-5` | 2.00 / 10.00 |
| Escalation on validation failure | `anthropic/claude-opus-5` | 5.00 / 25.00 |
| Exercise variations | `anthropic/claude-sonnet-5` | 2.00 / 10.00 |

All overridable in `.env`. OpenRouter's `:batch` model variants are 50% cheaper for any
bulk job, and `qwen/qwen3-30b-a3b-instruct-2507` ($0.05 / $0.19) is both the cheap
option and the size class that fits a 16GB GPU locally.

## Safety notes

- **Generated exercise variations go through a review gate.** They land in
  `staged_variations` with status `pending` and are only written into wger after you
  approve them. Recombining equipment × posture × grip × movement pattern can produce
  movements that are nonsensical or unsafe, and this stack writes into a real training log.
- **Routine payloads are validated against wger's OpenAPI schema before being sent**, so
  a malformed progression rule fails locally instead of half-writing a routine.
- **Never run wger's `sync-exercises` with a delete flag.** Imported exercises carry
  project-local UUIDs that are absent from upstream's deletion log.

## Status

See `plans/2026-07-30-ai-fitness-selfhost.md`. Phase 1 (extraction) and Phase 2
(stack + sidecar) are done; Phases 3–6 (wger import, agent service, variation review,
web UI) are next.
