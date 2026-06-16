"""fine-tunes: an A/B listening experiment harness for AI music generators."""
import os

from dotenv import load_dotenv
from flask import Flask

from .config import Config
from .models import db


def create_app(config=None):
    load_dotenv()
    app = Flask(
        __name__,
        template_folder=os.path.join(os.path.dirname(__file__), "..", "templates"),
        static_folder=os.path.join(os.path.dirname(__file__), "..", "static"),
    )
    app.config.from_object(config or Config())

    os.makedirs(app.config["AUDIO_STORAGE_DIR"], exist_ok=True)

    db.init_app(app)
    with app.app_context():
        db.create_all()

    from .routes import bp

    app.register_blueprint(bp)
    return app
