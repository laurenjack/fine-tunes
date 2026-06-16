from finetunes.models import db


def test_database_config_accepts_legacy_app_shape():
    class LegacyApp:
        config = {
            "SQLALCHEMY_DATABASE_URI": "sqlite:///legacy.db",
            "AUDIO_STORAGE_DIR": "/tmp/audio",
            "CLIP_SECONDS": 1,
        }
        instance_path = "/tmp/instance"
        root_path = "/tmp/root"

    settings = db._coerce_config(LegacyApp())

    assert settings.SQLALCHEMY_DATABASE_URI == "sqlite:///legacy.db"
    assert settings.AUDIO_STORAGE_DIR == "/tmp/audio"
    assert settings.INSTANCE_DIR == "/tmp/instance"
    assert settings.BASE_DIR == "/tmp/root"
