# fine-tunes

A blind A/B listening-test harness for AI music generators. Each prompt is
sampled N times; every sample generates a ~10s clip from **ElevenLabs Music**
and one from **Stable Audio 3.0** (via fal), plays them back blind in random order,
and you pick the one you prefer. After the experiment you get an overall
ElevenLabs win rate with a 95% Wilson confidence interval.

The rollout section captures on-policy RL preference data: each prompt produces
six outputs from the active rollout policy, the listener ranks the full set from
best to worst, and the app stores an exportable ranking record.

Single user (jacklaurenson@gmail.com) — no auth.

## Stack

- **FastAPI** backend + a small vanilla-JS frontend (no build step).
- **SQLAlchemy** over SQLite by default; point `DATABASE_URL` at Postgres to move hosts.
- Audio bytes are written to disk under `storage/<experiment_id>/`; the database
  stores each generation's provider, the exact request payload, the file path,
  format and duration.
- Deployment is intentionally left open (local-first). Because this is a stateful
  FastAPI server with a database and file storage, a long-running host (Render /
  Fly / Railway) or Vercel-functions + Supabase fits better than Cloudflare Pages.

## Quick start

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # add API keys when you have them
python app.py                 # http://127.0.0.1:5001
# or: uvicorn app:app --reload --port 5001
```

Without API keys (or with `FINETUNES_USE_MOCK=true`) the app uses a built-in
**mock generator** that synthesises distinct tones, so the entire flow is
usable offline. Drop real keys into `.env` to call the live services.

Rollouts default to `ROLLOUT_PROVIDER=elevenlabs`; set `ROLLOUT_POLICY_NAME`
to label the active LoRA/policy version in saved ranking exports.

## Data model

| Table         | Holds                                                              |
|---------------|-------------------------------------------------------------------|
| `experiments` | id (counter from 1), num_prompts, samples_per_prompt, clip_seconds |
| `prompts`     | the music descriptions you enter, ordered within an experiment     |
| `comparisons` | one A/B trial; the chosen slot + winning provider                  |
| `generations` | one clip: provider, request payload, audio path/format/duration    |
| `rollouts` | an on-policy ranking run: user, prompt count, policy, clip seconds |
| `rollout_prompts` | one rollout prompt plus `ranked_at` when its ranking is saved |
| `rollout_candidates` | six generated outputs with display slot and rank position |

## API

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/experiments` | overview rows for all experiments |
| POST | `/api/experiments` | create; returns id + url |
| GET | `/api/experiments/<id>/state` | current step (prompt entry / sampling / done) |
| POST | `/api/experiments/<id>/prompts` | add a prompt |
| POST | `/api/experiments/<id>/sample` | generate a blind pair for a prompt |
| POST | `/api/comparisons/<id>/choose` | record the preferred slot |
| GET | `/api/experiments/<id>/results` | win rate + Wilson CI |
| GET | `/api/audio/<gen_id>` | serve a clip (anonymous — no provider in URL) |
| GET | `/api/rollouts` | overview rows for rollout sessions |
| POST | `/api/rollouts` | create a rollout; returns id + url |
| GET | `/api/rollouts/<id>/state` | current step (prompt entry / generation / ranking / done) |
| POST | `/api/rollouts/<id>/prompts` | add a rollout prompt |
| POST | `/api/rollouts/<id>/generate` | generate six on-policy outputs |
| POST | `/api/rollout-prompts/<id>/rank` | persist a best-to-worst slot ranking |
| GET | `/api/rollouts/<id>/rankings` | RL-ready ranking export for one rollout |
| GET | `/api/rollouts/rankings` | RL-ready ranking export for all rollouts |
| GET | `/api/rollout-candidates/<id>/audio` | serve a rollout candidate clip |

## Tests

```bash
source venv/bin/activate
python -m pytest tests/
```

Tests force the mock generator, so they run without keys or network.

## Provider notes

Pinned to the latest model of each, as single constants in the provider files:

- **ElevenLabs Music v2** — `POST https://api.elevenlabs.io/v1/music`, header
  `xi-api-key`, JSON `{prompt, music_length_ms, model_id: "music_v2"}`,
  `?output_format=mp3_44100_128`. (`MODEL_ID` in `finetunes/providers/elevenlabs.py`.)
- **Stable Audio 3.0 via fal** — `POST https://fal.run/fal-ai/stable-audio-3/medium/text-to-audio`,
  header `Authorization: Key <FAL_KEY>`, JSON `{prompt, duration, output_format}`;
  the response JSON gives `audio.url`, which we download.
  (`ENDPOINT` in `finetunes/providers/fal_stable_audio.py`.)

Both verified live. If either key is absent, that side falls back to the local
mock generator so the flow still runs.
