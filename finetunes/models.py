"""Database models.

Two flavours of experiment share one schema:

  Experiment(kind='head_to_head') compares two models. Each Prompt produces
  one Comparison with six Generations: three from model_a and three from
  model_b. The listener ranks all six (1=best..6=worst). The result reports
  the cross-pair win rate (Mann-Whitney U) with a Wilson 95% CI.

  Experiment(kind='rollout') produces six on-policy candidates from a single
  model (model_a). The listener ranks all six; the data is exported as
  preference pairs for RL preference training. model_b is null.

When model_a == model_b in a head_to_head experiment, slot assignment is
*deterministic* (side 'a' goes to slots 1-3, side 'b' to slots 4-6) so the
win rate becomes a position-bias check ("did slots 1-3 systematically beat
slots 4-6?"). Otherwise slots are shuffled to hide which side is which.
"""
import os
from contextvars import ContextVar
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import declarative_base, relationship, scoped_session, sessionmaker

_session_scope = ContextVar("finetunes_session_scope", default=None)

CANDIDATES_PER_PROMPT = 6
HALF_CANDIDATES = CANDIDATES_PER_PROMPT // 2  # 3 per side for head-to-head

KIND_HEAD_TO_HEAD = "head_to_head"
KIND_ROLLOUT = "rollout"
KINDS = (KIND_HEAD_TO_HEAD, KIND_ROLLOUT)


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
    """Small SQLAlchemy facade for `db.session` / `db.Model` call sites."""

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

    id = db.Column(db.Integer, primary_key=True)
    user_email = db.Column(db.String(255), nullable=False)
    # 'head_to_head' (two models, 3+3 candidates) or 'rollout' (one model, 6).
    kind = db.Column(db.String(16), nullable=False)
    model_a = db.Column(db.String(64), nullable=False)   # provider name; always set
    model_b = db.Column(db.String(64), nullable=True)    # null for rollout
    num_prompts = db.Column(db.Integer, nullable=False)
    candidates_per_prompt = db.Column(db.Integer, nullable=False, default=CANDIDATES_PER_PROMPT)
    clip_seconds = db.Column(db.Integer, nullable=False, default=10)
    created_at = db.Column(db.DateTime, default=_utcnow)

    prompts = db.relationship(
        "Prompt", backref="experiment", order_by="Prompt.order_index"
    )

    @property
    def is_rollout(self):
        return self.kind == KIND_ROLLOUT

    @property
    def is_same_model(self):
        return self.kind == KIND_HEAD_TO_HEAD and self.model_a == self.model_b


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
        "Comparison", backref="prompt", order_by="Comparison.id"
    )


class Comparison(db.Model):
    """One ranking session per prompt. Holds the 6 candidates."""
    __tablename__ = "comparisons"

    id = db.Column(db.Integer, primary_key=True)
    experiment_id = db.Column(
        db.Integer, db.ForeignKey("experiments.id"), nullable=False, index=True
    )
    prompt_id = db.Column(
        db.Integer, db.ForeignKey("prompts.id"), nullable=False, index=True
    )
    created_at = db.Column(db.DateTime, default=_utcnow)
    ranked_at = db.Column(db.DateTime, nullable=True)

    generations = db.relationship(
        "Generation", backref="comparison", order_by="Generation.slot"
    )

    @property
    def is_ranked(self):
        return self.ranked_at is not None


class Generation(db.Model):
    __tablename__ = "generations"

    id = db.Column(db.Integer, primary_key=True)
    comparison_id = db.Column(
        db.Integer, db.ForeignKey("comparisons.id"), nullable=False, index=True
    )
    # Display slot the listener sees (1..6).
    slot = db.Column(db.Integer, nullable=False)
    # Which model produced this clip (always a real provider name).
    provider = db.Column(db.String(64), nullable=False)
    # 'a' or 'b' — which side of the experiment this belongs to.
    # For rollout all generations are 'a'. For head-to-head with model_a==model_b
    # the side label still partitions the six candidates into two halves so the
    # win rate can detect position bias.
    side = db.Column(db.String(1), nullable=False)
    # 1=best..6=worst within this comparison. Null until the user submits the
    # full six-way ranking.
    rank_position = db.Column(db.Integer, nullable=True)
    prompt_text = db.Column(db.Text, nullable=False)
    request_payload = db.Column(db.Text, nullable=False)
    audio_path = db.Column(db.String(512), nullable=True)
    audio_format = db.Column(db.String(16), nullable=True)  # 'mp3' | 'wav'
    duration_ms = db.Column(db.Integer, nullable=True)
    status = db.Column(db.String(16), nullable=False, default="pending")  # ok|error
    error = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow)
