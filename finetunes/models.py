"""Database models.

An Experiment has N Prompts. Each Prompt is compared M (samples_per_prompt)
times. Each Comparison pairs two Generations (one per provider) presented in a
random slot order (1 or 2) so the listener cannot tell which API made which.
The listener picks a winning slot; we record which provider that was.
"""
from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


def _utcnow():
    return datetime.now(timezone.utc)


class Experiment(db.Model):
    __tablename__ = "experiments"

    # Auto-incrementing integer, starts at 1 for the first experiment.
    id = db.Column(db.Integer, primary_key=True)
    user_email = db.Column(db.String(255), nullable=False)
    num_prompts = db.Column(db.Integer, nullable=False)
    samples_per_prompt = db.Column(db.Integer, nullable=False)
    clip_seconds = db.Column(db.Integer, nullable=False, default=10)
    created_at = db.Column(db.DateTime, default=_utcnow)

    prompts = db.relationship(
        "Prompt", backref="experiment", order_by="Prompt.order_index"
    )


class Prompt(db.Model):
    __tablename__ = "prompts"

    id = db.Column(db.Integer, primary_key=True)
    experiment_id = db.Column(
        db.Integer, db.ForeignKey("experiments.id"), nullable=False, index=True
    )
    order_index = db.Column(db.Integer, nullable=False)  # 0-based within experiment
    text = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=_utcnow)

    comparisons = db.relationship(
        "Comparison", backref="prompt", order_by="Comparison.sample_index"
    )


class Comparison(db.Model):
    __tablename__ = "comparisons"

    id = db.Column(db.Integer, primary_key=True)
    experiment_id = db.Column(
        db.Integer, db.ForeignKey("experiments.id"), nullable=False, index=True
    )
    prompt_id = db.Column(
        db.Integer, db.ForeignKey("prompts.id"), nullable=False, index=True
    )
    sample_index = db.Column(db.Integer, nullable=False)  # 0-based within prompt
    created_at = db.Column(db.DateTime, default=_utcnow)

    # Set once the listener chooses. winner_slot is 1 or 2; winner_provider is
    # the provider name behind that slot.
    winner_slot = db.Column(db.Integer, nullable=True)
    winner_provider = db.Column(db.String(64), nullable=True)
    decided_at = db.Column(db.DateTime, nullable=True)

    generations = db.relationship(
        "Generation", backref="comparison", order_by="Generation.slot"
    )

    @property
    def is_decided(self):
        return self.winner_slot is not None


class Generation(db.Model):
    __tablename__ = "generations"

    id = db.Column(db.Integer, primary_key=True)
    comparison_id = db.Column(
        db.Integer, db.ForeignKey("comparisons.id"), nullable=False, index=True
    )
    # The slot the listener sees this in (1 or 2), randomised per comparison.
    slot = db.Column(db.Integer, nullable=False)
    provider = db.Column(db.String(64), nullable=False)  # "elevenlabs" | "stable_audio"
    prompt_text = db.Column(db.Text, nullable=False)
    # The exact request we sent to the provider, as JSON text.
    request_payload = db.Column(db.Text, nullable=False)
    audio_path = db.Column(db.String(512), nullable=True)
    audio_format = db.Column(db.String(16), nullable=True)  # "mp3" | "wav"
    duration_ms = db.Column(db.Integer, nullable=True)
    status = db.Column(db.String(16), nullable=False, default="pending")  # ok|error
    error = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow)
