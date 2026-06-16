"""Provider registry and resolution.

The two providers being compared are always "elevenlabs" and "stable_audio".
`get_provider` returns the real implementation when its API key is present and
mocking is off; otherwise it returns a MockProvider that keeps the same `name`
so the database still records which provider was *intended*.

  elevenlabs   -> ElevenLabs Music v2        (ELEVENLABS_API_KEY)
  stable_audio -> Stable Audio 3.0 via fal   (FAL_KEY)
"""
import os

from .elevenlabs import ElevenLabsProvider
from .fal_stable_audio import FalStableAudioProvider
from .mock import MockProvider

PROVIDER_NAMES = ("elevenlabs", "stable_audio")

_REAL = {
    "elevenlabs": ElevenLabsProvider,
    "stable_audio": FalStableAudioProvider,
}
_KEY_ENV = {
    "elevenlabs": "ELEVENLABS_API_KEY",
    "stable_audio": "FAL_KEY",
}


def _mock_forced():
    return os.environ.get("FINETUNES_USE_MOCK", "").strip().lower() in ("1", "true", "yes")


def get_provider(name):
    if name not in _REAL:
        raise ValueError("Unknown provider: %s" % name)
    if _mock_forced() or not os.environ.get(_KEY_ENV[name]):
        return MockProvider(name)
    return _REAL[name]()


def is_mock(name):
    """Whether `name` will currently resolve to the mock generator."""
    if name not in _KEY_ENV:
        return True
    return _mock_forced() or not os.environ.get(_KEY_ENV[name])


def active_backend(name):
    """Human-readable description of what `name` resolves to right now."""
    return get_provider(name).display_name
