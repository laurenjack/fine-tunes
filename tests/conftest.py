import os
import tempfile

import pytest
from fastapi.testclient import TestClient

# Force the mock generator so tests never hit a real API.
os.environ["FINETUNES_USE_MOCK"] = "true"

from finetunes import create_app  # noqa: E402
from finetunes.config import Config  # noqa: E402
from finetunes.models import db  # noqa: E402


@pytest.fixture()
def app():
    tmp = tempfile.mkdtemp()
    cfg = Config()
    cfg.SQLALCHEMY_DATABASE_URI = "sqlite:///" + os.path.join(tmp, "test.db")
    cfg.AUDIO_STORAGE_DIR = os.path.join(tmp, "audio")
    cfg.CLIP_SECONDS = 1  # keep mock synthesis fast in tests
    app = create_app(cfg)
    yield app
    db.session.remove()


@pytest.fixture()
def client(app):
    return TestClient(app)
