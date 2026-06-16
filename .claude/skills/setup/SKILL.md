---
name: setup
description: Set up the fine-tunes repo for local development — create the virtualenv, install dependencies, scaffold .env, and direct the developer to add their API keys.
---

Get a fresh checkout of fine-tunes ready to run. Do the mechanical setup yourself, then guide the developer through the manual `.env` step (you cannot — and must not — fill in their secrets).

## Steps

1. **Run the setup script** (creates `venv/`, installs deps, copies `.env.example` → `.env` if missing):

   ```bash
   ./scripts/setup.sh
   ```

   If `python3` is not the right interpreter, pass one: `PYTHON=python3.11 ./scripts/setup.sh`.

2. **Direct the developer to fill in `.env`.** It was just scaffolded from `.env.example`. Tell them to open `.env` and set:

   - `ELEVENLABS_API_KEY` — from the ElevenLabs dashboard (used for Music v2).
   - `FAL_KEY` — from https://fal.ai/dashboard/keys (used for Stable Audio 3.0).

   Optional:
   - `DATABASE_URL` — defaults to `sqlite:///finetunes.db` (a local file). Point it at Postgres to use another store.
   - `FINETUNES_USE_MOCK=true` — run the whole flow with a local mock generator, no keys or credits needed.

   **You must not read or edit `.env` yourself** — it holds secrets and is blocked by a deny rule in `.claude/settings.json`. Ask the developer to add the keys; if they have none yet, suggest setting `FINETUNES_USE_MOCK=true` so they can still try the app.

3. **Verify the install** (uses the mock generator, so no keys/network needed):

   ```bash
   FINETUNES_USE_MOCK=true ./venv/bin/python -m pytest tests/ -q
   ```

   All tests should pass. If imports fail, the venv didn't build — re-run step 1.

4. **Hand off.** Tell the developer that once their keys are in `.env`, they can start the app with the `/start_dev` skill (or `./scripts/start_dev.sh`).

## Notes

- This is local-first: SQLite + audio files on disk under `storage/`. Nothing here deploys to a server.
- A missing key isn't fatal — that provider falls back to the mock generator, and the app still runs end to end.
