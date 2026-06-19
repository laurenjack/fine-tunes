# fine-tunes

A local tool for collecting blind preference data on AI music generators.

Every experiment generates **6 anonymous candidates per prompt** and asks the
listener to rank them from best to worst by drag-and-drop. Two flavours share
one schema and one UI:

- **Head-to-head** — 3 candidates each from two models, slot-shuffled. Scored
  as the cross-pair win rate (Mann-Whitney U) with a 95% Wilson confidence
  interval. When the same model is picked on both sides, the slot layout is
  deterministic (side A → slots 1-3, side B → slots 4-6) so the win rate
  becomes a position-bias check.
- **Rollout** — all 6 candidates from one model. Each ranked prompt yields 15
  preference pairs, exportable as JSON for RL preference training.

Two users (`jacklaurenson@gmail.com`, `james.richardson.2556@gmail.com`) — no auth.

## Stack

- **FastAPI** + uvicorn backend, Jinja2 templates, a small vanilla-JS frontend
  (no build step, no framework). Static URLs are mtime-cache-busted so JS/CSS
  edits never serve a stale browser cache.
- **SQLAlchemy** over SQLite by default; point `DATABASE_URL` at Postgres to
  move hosts.
- Audio bytes live on disk under `storage/<experiment_id>/`; the DB stores
  each generation's provider, the exact request payload sent, the file path,
  format and duration.
- **Provider concurrency** is capped per-tier so ElevenLabs's `429
  too_many_concurrent_requests` is impossible. Transient `system_busy` / 5xx
  are retried with exponential backoff.
- Deployment is intentionally left open (local-first). Stateful FastAPI + DB +
  files fits a long-running host (Render / Fly / Railway) or
  Vercel-functions + Supabase; *not* Cloudflare Pages (static-only).

## Quick start

The repo ships with two skills (`.claude/skills/setup`, `.claude/skills/start_dev`)
and the same scripts they wrap:

```bash
./scripts/setup.sh          # create venv, install deps, scaffold .env
# Edit .env and add:
#   ELEVENLABS_API_KEY=...     (ElevenLabs dashboard)
#   FAL_KEY=...                (https://fal.ai/dashboard/keys)
./scripts/start_dev.sh      # uvicorn at http://127.0.0.1:5001 (auto-reload)
```

Without keys (or with `FINETUNES_USE_MOCK=true`) both providers fall back to
the local mock generator that synthesises distinct tones, so the full UI flow
is usable offline.

Optional env knobs:

| Var | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite:///finetunes.db` | swap for Postgres etc. |
| `AUDIO_STORAGE_DIR` | `storage` | where audio clips are written |
| `CLIP_SECONDS` | `10` | clip length |
| `ELEVENLABS_MAX_CONCURRENCY` | `2` (Free-tier safe) | raise to your tier cap (Starter 3, Creator 5, Pro 10, Scale/Business 15) |
| `FAL_MAX_CONCURRENCY` | `4` | fal queue width |
| `FINETUNES_USE_MOCK` | `false` | force mock generator |

## How it flows

1. **Pick a kind on `/experiments`.** Kind switches between Head-to-head (two
   model pickers, A == B allowed) and Rollout (one model picker). Choose the
   user and number of prompts; click Run.
2. **Enter a prompt.** The server fires 6 generations in parallel — 3 from
   each provider for H2H, 6 from the one provider for rollout — under the
   per-provider concurrency cap. A spinner shows during generation.
3. **Rank.** The 6 clips appear as a single vertical stack of draggable bars,
   each labelled A-F (stable; the letter never moves). Drag any bar up or
   down; top = rank 1 (best), bottom = rank 6. The letter labels are
   identifiers, not the rank, so you can refer to "clip C" unambiguously.
4. **Submit.** The ranking is recorded and the next prompt's input opens.
   Repeat until all prompts are ranked.
5. **Results page.** H2H shows cross-pair win rate + Wilson CI. Rollout shows
   the preference-pair total and a JSON download link.

## Schema

| Table | Holds |
|---|---|
| `experiments` | id, user_email, **kind**, **model_a**, **model_b** (null for rollout), num_prompts, candidates_per_prompt (6), clip_seconds |
| `prompts` | one row per prompt entered, with `order_index` |
| `comparisons` | one ranking session per prompt; `ranked_at` set when submitted |
| `generations` | one clip: provider, **side** ('a'/'b'), **rank_position** (1-6), slot, request_payload, audio_path/format/duration_ms, status |

Schema changes are net-new on this project — see commit history for the
migration from the old "winner-pick" pairwise schema. The old `rollout_*`
tables, the `winner_slot` field, and `samples_per_prompt` are gone.

## Scoring

**Head-to-head, different models.** Each ranked prompt of 6 yields 9
cross-model pairs (3 × 3). A "wins" a pair iff its candidate ranks higher.
Total win rate ∈ [0, 1] with a Wilson 95% interval — this is the
Mann-Whitney U statistic dressed as a proportion.

**Head-to-head, same model (position-bias mode).** When model_a == model_b,
slot assignment is deterministic: side A occupies slots 1-3, side B occupies
slots 4-6. The same cross-pair win rate now answers "did the listener
systematically prefer slots 1-3?" A win rate ≈ 50% is healthy; skew exposes UI
position bias.

**Rollout.** No A/B contrast — the ranking *is* the data. Each ranked prompt
yields C(6, 2) = 15 ordered preference pairs of the form `(preferred,
rejected)`, exposed via `GET /api/experiments/<id>/preferences` as JSON
suitable for RLHF / DPO reward modelling.

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | redirects to `/experiments` |
| GET | `/experiments` | start form + two history tables |
| GET | `/experiment/<id>` | the rank-this-prompt page |
| GET | `/experiment/<id>/results` | scoring + (rollout) export link |
| GET | `/api/experiments` | overview rows split into `head_to_head` / `rollout` |
| POST | `/api/experiments` | create; body: `{user_email, kind, model_a, model_b?, num_prompts}` |
| GET | `/api/experiments/<id>/state` | current phase: prompt-entry / generating / ranking / done |
| POST | `/api/experiments/<id>/prompts` | add a prompt |
| POST | `/api/experiments/<id>/generate` | generate 6 candidates for a prompt (idempotent, self-healing) |
| POST | `/api/comparisons/<id>/rank` | submit a full ranking: `{ranked_slots: [slot, ...]}` |
| GET | `/api/experiments/<id>/results` | win rate / preference pair count |
| GET | `/api/experiments/<id>/preferences` | rollout-only: preference-pair JSON export |
| GET | `/api/audio/<gen_id>` | serve one clip (anonymous — no provider in URL) |

## Provider notes

Pinned to the latest model of each, as single constants in the provider files:

- **ElevenLabs Music v2** — `POST https://api.elevenlabs.io/v1/music`, header
  `xi-api-key`, JSON `{prompt, music_length_ms, model_id: "music_v2",
  force_instrumental: true}`. (`MODEL_ID` in
  `finetunes/providers/elevenlabs.py`.)
- **Stable Audio 3.0 (medium) via fal** —
  `POST https://fal.run/fal-ai/stable-audio-3/medium/text-to-audio`,
  header `Authorization: Key <FAL_KEY>`, JSON `{prompt, duration,
  output_format}`. fal responds with `audio.url`, which we download.
  (`ENDPOINT` in `finetunes/providers/fal_stable_audio.py`.)

If either key is absent the corresponding side falls back to the local mock
generator so the full UI flow still runs.

## Tests

```bash
FINETUNES_USE_MOCK=true ./venv/bin/python -m pytest tests/ -q
```

Tests force the mock generator, so they run without keys or network. The
suite covers the H2H end-to-end + scoring (Mann-Whitney win rate, Wilson CI),
same-model position-bias slot layout, rollout end-to-end + preference-pair
export, ranking validation, idempotent + concurrent generation, and overview
rendering. The `crosspair_wins` helper has its own unit tests.

## Project layout

```
finetunes/              # the Python package
  __init__.py             # create_app() — FastAPI factory
  config.py               # USERS + Config (env-driven)
  models.py               # SQLAlchemy models + _Database facade
  routes.py               # all HTTP routes (pages + JSON API)
  service.py              # generation + ranking + scoring logic
  stats.py                # wilson_interval, crosspair_wins
  providers/
    elevenlabs.py
    fal_stable_audio.py
    mock.py
templates/              # Jinja2 templates (base, experiments, experiment, results)
static/                 # vanilla JS + CSS (no build step)
scripts/                # setup.sh, start_dev.sh, check_providers.py
tests/                  # pytest suite (mock generator)
storage/                # audio bytes per experiment (gitignored)
instance/               # SQLite DB lives here (gitignored)
```
