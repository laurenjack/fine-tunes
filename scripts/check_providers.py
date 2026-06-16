"""Fire one real request at each provider to verify endpoints/models.

Loads .env inside Python (keys never printed). Prints only status, format,
byte count, and the request payload (endpoint + model — no secrets). Writes
each clip to storage/_check/ so you can listen to them.

    source venv/bin/activate && python scripts/check_providers.py
"""
import json
import os
import sys

from dotenv import load_dotenv

# Ensure the repo root is importable and load .env.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv()

from finetunes.providers import PROVIDER_NAMES, get_provider, is_mock  # noqa: E402

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "storage", "_check")
PROMPT = "a short calm ambient piano motif"
DURATION = 10


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    for name in PROVIDER_NAMES:
        print("=" * 60)
        print("provider:", name, "(MOCK)" if is_mock(name) else "(REAL)")
        provider = get_provider(name)
        try:
            result = provider.generate(PROMPT, DURATION)
        except Exception as exc:  # noqa: BLE001
            # requests' error messages include the URL + status, never headers,
            # so this is safe to print.
            print("  STATUS: FAILED")
            print("  error:", type(exc).__name__, str(exc))
            continue
        path = os.path.join(OUT_DIR, "%s.%s" % (name, result.audio_format))
        with open(path, "wb") as f:
            f.write(result.audio_bytes)
        print("  STATUS: OK")
        print("  format:", result.audio_format, "| bytes:", len(result.audio_bytes),
              "| duration_ms:", result.duration_ms)
        print("  request:", json.dumps(result.request_payload))
        print("  saved:", path)


if __name__ == "__main__":
    main()
