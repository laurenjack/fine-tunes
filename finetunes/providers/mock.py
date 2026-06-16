"""Local mock provider.

Synthesises a short WAV tone using only the standard library so the entire
experiment flow works without any API keys or credits. Each provider gets a
distinct base frequency so the two clips are audibly different while testing.
"""
import io
import math
import struct
import wave

from .base import AudioResult, Provider

_SAMPLE_RATE = 22050
# A recognisable, slightly different timbre per provider name.
_BASE_FREQ = {"elevenlabs": 392.0, "stable_audio": 523.25}  # G4 vs C5


def _synthesise_wav(freq, seconds):
    n = int(_SAMPLE_RATE * seconds)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(_SAMPLE_RATE)
        frames = bytearray()
        for i in range(n):
            t = i / _SAMPLE_RATE
            # Two-tone chord with a gentle fade so it reads as "music-ish".
            sample = 0.5 * math.sin(2 * math.pi * freq * t)
            sample += 0.25 * math.sin(2 * math.pi * freq * 1.5 * t)
            envelope = min(1.0, t * 4) * min(1.0, (seconds - t) * 4)
            value = int(max(-1.0, min(1.0, sample * envelope)) * 32767)
            frames += struct.pack("<h", value)
        w.writeframes(bytes(frames))
    return buf.getvalue()


class MockProvider(Provider):
    """Stands in for a real provider, keeping its `name` for the records."""

    def __init__(self, name):
        self.name = name
        self.display_name = "Mock (%s)" % name

    def generate(self, prompt, duration_seconds):
        freq = _BASE_FREQ.get(self.name, 440.0)
        audio = _synthesise_wav(freq, duration_seconds)
        payload = {
            "endpoint": "mock://%s" % self.name,
            "method": "LOCAL",
            "params": {
                "prompt": prompt,
                "duration_seconds": duration_seconds,
                "frequency_hz": freq,
            },
        }
        return AudioResult(audio, "wav", payload, int(duration_seconds * 1000))
