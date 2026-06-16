---
name: start_dev
description: Start the fine-tunes Flask dev server (auto-reloads on code changes) at http://127.0.0.1:5001.
---

Start the local development server. It auto-reloads when you edit Python, templates, or static files, so most changes show up on a browser refresh without a manual restart.

## Steps

1. **Start it in the background** (the script frees port 5001 first, then runs the Flask dev server):

   ```bash
   ./scripts/start_dev.sh
   ```

   Run this with `run_in_background: true` so it keeps serving across turns. Then report to the developer:
   - URL: **http://127.0.0.1:5001**
   - It auto-reloads on code changes (Flask debug reloader).
   - To stop it: `pkill -f "finetunes import create_app"` (or re-running the script frees the port and restarts).

2. **Confirm it's up** before handing off:

   ```bash
   sleep 2 && curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:5001/
   ```

   A `200` means it's serving. If it's not 200, check the background output for a traceback (a missing dependency means `/setup` wasn't run; a port clash means an old server is still bound).

## Notes

- Override the port with `PORT=8000 ./scripts/start_dev.sh` if 5001 is taken.
- Secrets come from `.env` (loaded automatically). If keys are absent the app still runs using the mock generator.
- This is the dev server only — fine for local use, not production.
