"""Application configuration, read from environment variables."""
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Known users. The first entry is the default selection in the UI.
USERS = [
    "james.richardson.2556@gmail.com",
    "jacklaurenson@gmail.com",
]
DEFAULT_USER = USERS[0]


class Config:
    def __init__(self):
        self.BASE_DIR = BASE_DIR
        self.INSTANCE_DIR = os.path.join(BASE_DIR, "instance")
        self.SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")
        self.SQLALCHEMY_DATABASE_URI = os.environ.get(
            "DATABASE_URL", "sqlite:///finetunes.db"
        )
        self.SQLALCHEMY_TRACK_MODIFICATIONS = False
        # Absolute path to the directory where audio files are stored.
        storage = os.environ.get("AUDIO_STORAGE_DIR", "storage")
        self.AUDIO_STORAGE_DIR = os.path.abspath(storage)
        # Each generated song is this many seconds long.
        self.CLIP_SECONDS = int(os.environ.get("CLIP_SECONDS", "10"))
