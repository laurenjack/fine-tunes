"""Database models.

An Experiment has N Prompts. Each Prompt is compared M (samples_per_prompt)
times. Each Comparison pairs two Generations (one per provider) presented in a
random slot order (1 or 2) so the listener cannot tell which API made which.
The listener picks a winning slot; we record which provider that was.

A Rollout captures on-policy preference data: one prompt, six candidates from
the same policy, and a full user ranking for RL training/export.
"""
import os
from contextvars import ContextVar
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import declarative_base, relationship, scoped_session, sessionmaker

_session_scope = ContextVar("finetunes_session_scope", default=None)


class _ConfigMapping:
    def __init__(self, mapping, instance_dir=None, base_dir=None):
        self._mapping = mapping
        self._instance_dir = instance_dir
        self._base_dir = base_dir

    def __getattr__(self, name):
        if name in self._mapping:
            return self._mapping[name]
        if name == "INSTANCE_DIR" and self._instance_dir:
            return self._instance_dir
        if name == "BASE_DIR" and self._base_dir:
            return self._base_dir
        raise AttributeError(name)


class _Database:
    """Small SQLAlchemy facade for the old `db.session` call sites."""

    def __init__(self):
        self.Model = declarative_base()
        self.session = scoped_session(
            sessionmaker(autocommit=False, autoflush=False),
            scopefunc=lambda: _session_scope.get(),
        )
        self.engine = None
        self.config = None

        self.Column = Column
        self.DateTime = DateTime
        self.ForeignKey = ForeignKey
        self.Integer = Integer
        self.String = String
        self.Text = Text
        self.relationship = relationship

    def init_app(self, config):
        self.session.remove()
        if self.engine is not None:
            self.engine.dispose()

        settings = self._coerce_config(config)
        self.config = settings
        uri = self._database_uri(settings)
        connect_args = {}
        if uri.startswith("sqlite:"):
            connect_args["check_same_thread"] = False
            self._ensure_sqlite_parent_dir(uri)

        self.engine = create_engine(uri, connect_args=connect_args)
        self.session.configure(bind=self.engine)
        self.Model.query = self.session.query_property()

    @staticmethod
    def _coerce_config(config):
        if hasattr(config, "SQLALCHEMY_DATABASE_URI"):
            return config
        if hasattr(config, "config"):
            return _ConfigMapping(
                config.config,
                instance_dir=getattr(config, "instance_path", None),
                base_dir=getattr(config, "root_path", None),
            )
        if isinstance(config, dict):
            return _ConfigMapping(config)
        return config

    def create_all(self):
        if self.engine is None:
            raise RuntimeError("Database has not been initialised")
        self.Model.metadata.create_all(self.engine)

    def drop_all(self):
        if self.engine is None:
            raise RuntimeError("Database has not been initialised")
        self.Model.metadata.drop_all(self.engine)

    def begin_request(self):
        return _session_scope.set(object())

    def end_request(self, token):
        self.session.remove()
        _session_scope.reset(token)

    def _database_uri(self, config):
        uri = config.SQLALCHEMY_DATABASE_URI
        url = make_url(uri)
        database = url.database
        if (
            url.drivername.startswith("sqlite")
            and database
            and database != ":memory:"
            and not os.path.isabs(database)
        ):
            instance_dir = getattr(config, "INSTANCE_DIR", os.path.abspath("instance"))
            return "sqlite:///" + os.path.join(instance_dir, database)
        return uri

    @staticmethod
    def _ensure_sqlite_parent_dir(uri):
        url = make_url(uri)
        database = url.database
        if database and database != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(database)), exist_ok=True)


db = _Database()


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


class Rollout(db.Model):
    __tablename__ = "rollouts"

    id = db.Column(db.Integer, primary_key=True)
    user_email = db.Column(db.String(255), nullable=False)
    num_prompts = db.Column(db.Integer, nullable=False)
    outputs_per_prompt = db.Column(db.Integer, nullable=False, default=6)
    clip_seconds = db.Column(db.Integer, nullable=False, default=10)
    provider = db.Column(db.String(64), nullable=False)
    policy_name = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=_utcnow)

    prompts = db.relationship(
        "RolloutPrompt", backref="rollout", order_by="RolloutPrompt.order_index"
    )


class RolloutPrompt(db.Model):
    __tablename__ = "rollout_prompts"

    id = db.Column(db.Integer, primary_key=True)
    rollout_id = db.Column(
        db.Integer, db.ForeignKey("rollouts.id"), nullable=False, index=True
    )
    order_index = db.Column(db.Integer, nullable=False)
    text = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=_utcnow)
    ranked_at = db.Column(db.DateTime, nullable=True)

    candidates = db.relationship(
        "RolloutCandidate", backref="prompt", order_by="RolloutCandidate.slot"
    )

    @property
    def is_ranked(self):
        return self.ranked_at is not None


class RolloutCandidate(db.Model):
    __tablename__ = "rollout_candidates"

    id = db.Column(db.Integer, primary_key=True)
    rollout_prompt_id = db.Column(
        db.Integer, db.ForeignKey("rollout_prompts.id"), nullable=False, index=True
    )
    # The display slot the listener sees (1..6), randomised per prompt.
    slot = db.Column(db.Integer, nullable=False)
    provider = db.Column(db.String(64), nullable=False)
    policy_name = db.Column(db.String(255), nullable=False)
    prompt_text = db.Column(db.Text, nullable=False)
    request_payload = db.Column(db.Text, nullable=False)
    audio_path = db.Column(db.String(512), nullable=True)
    audio_format = db.Column(db.String(16), nullable=True)
    duration_ms = db.Column(db.Integer, nullable=True)
    status = db.Column(db.String(16), nullable=False, default="pending")
    error = db.Column(db.Text, nullable=True)
    # 1 is best. Null until the user submits the full six-way ranking.
    rank_position = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow)
