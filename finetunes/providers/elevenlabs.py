"""ElevenLabs Music API provider.

POST https://api.elevenlabs.io/v1/music
  header: xi-api-key
  query:  output_format=mp3_44100_128
  json:   {prompt, music_length_ms, model_id}
  returns: audio bytes (mp3)
Docs: https://elevenlabs.io/docs/api-reference/music/compose

Pinned to the latest model, Music v2 (launched 2026-06-11). NOTE: during the
v2 transition the simple /v1/music compose endpoint's published schema may still
only accept "music_v1"; if a 400 comes back, change MODEL_ID below to "music_v1".
"""
import os

import requests

from .base import AudioResult, Provider

ENDPOINT = "https://api.elevenlabs.io/v1/music"
MODEL_ID = "music_v2"  # latest


class ElevenLabsProvider(Provider):
    name = "elevenlabs"
    display_name = "ElevenLabs Music"

    def generate(self, prompt, duration_seconds):
        api_key = os.environ["ELEVENLABS_API_KEY"]
        model_id = MODEL_ID
        duration_ms = int(duration_seconds * 1000)
        output_format = "mp3_44100_128"

        body = {
            "prompt": prompt,
            "music_length_ms": duration_ms,
            "model_id": model_id,
            # Guarantee instrumental output (no vocals/lyrics).
            "force_instrumental": True,
        }
        payload = {
            "endpoint": ENDPOINT,
            "method": "POST",
            "query": {"output_format": output_format},
            "json": body,
        }
        resp = requests.post(
            ENDPOINT,
            headers={"xi-api-key": api_key, "Content-Type": "application/json"},
            params={"output_format": output_format},
            json=body,
            timeout=300,
        )
        resp.raise_for_status()
        return AudioResult(resp.content, "mp3", payload, duration_ms)
