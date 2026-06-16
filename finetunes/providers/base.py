"""Provider interface and the result object every provider returns."""


class AudioResult:
    def __init__(self, audio_bytes, audio_format, request_payload, duration_ms):
        self.audio_bytes = audio_bytes
        self.audio_format = audio_format  # "mp3" | "wav"
        self.request_payload = request_payload  # dict, serialised to JSON for storage
        self.duration_ms = duration_ms


class Provider:
    name = "base"
    display_name = "Base"

    def generate(self, prompt, duration_seconds):
        """Generate audio for `prompt`. Returns an AudioResult or raises."""
        raise NotImplementedError
