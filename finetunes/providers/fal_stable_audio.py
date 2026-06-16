"""Stable Audio 3.0 via fal.ai.

fal hosts the open-weight Stable Audio 3 (medium). Two-step: POST returns JSON
pointing at a hosted audio file, then we download the bytes.

POST https://fal.run/fal-ai/stable-audio-3/medium/text-to-audio
  header: Authorization: Key <FAL_KEY>, Content-Type: application/json
  json:   {prompt, duration (seconds), output_format, num_inference_steps}
  resp:   {"audio": {"url": ...}, "seed": ...}
Docs: https://fal.ai/models/fal-ai/stable-audio-3/medium/text-to-audio/api
"""
import os

import requests

from .base import AudioResult, Provider

ENDPOINT = "https://fal.run/fal-ai/stable-audio-3/medium/text-to-audio"
MODEL = "stable-audio-3-medium"


class FalStableAudioProvider(Provider):
    name = "stable_audio"  # same logical slot as the native provider
    display_name = "Stable Audio 3.0 (fal)"

    def generate(self, prompt, duration_seconds):
        api_key = os.environ["FAL_KEY"]
        output_format = "mp3"
        duration = int(duration_seconds)

        body = {
            "prompt": prompt,
            "duration": duration,
            "output_format": output_format,
        }
        payload = {
            "endpoint": ENDPOINT,
            "method": "POST",
            "model": MODEL,
            "json": body,
        }
        resp = requests.post(
            ENDPOINT,
            headers={
                "authorization": "Key " + api_key,
                "content-type": "application/json",
            },
            json=body,
            timeout=300,
        )
        resp.raise_for_status()
        data = resp.json()
        audio_url = data.get("audio", {}).get("url")
        if not audio_url:
            raise RuntimeError("fal response had no audio.url: %s" % data)

        # Download the generated file. The key is not needed for the CDN URL.
        audio_resp = requests.get(audio_url, timeout=300)
        audio_resp.raise_for_status()
        payload["audio_url"] = audio_url
        return AudioResult(audio_resp.content, output_format, payload, duration * 1000)
