"""HTTP routes: pages + JSON API."""
import os

from flask import (
    Blueprint,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from . import service
from .config import DEFAULT_USER, USERS
from .models import Comparison, Experiment, Generation, Prompt, db
from .providers import is_mock

bp = Blueprint("main", __name__)

_MIME = {"mp3": "audio/mpeg", "wav": "audio/wav"}


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
@bp.route("/")
def index():
    mocking = is_mock("elevenlabs") or is_mock("stable_audio")
    return render_template(
        "index.html", mocking=mocking, users=USERS, default_user=DEFAULT_USER
    )


@bp.route("/experiment/<int:exp_id>")
def experiment_page(exp_id):
    exp = Experiment.query.get_or_404(exp_id)
    return render_template("experiment.html", experiment=exp)


@bp.route("/experiment/<int:exp_id>/results")
def results_page(exp_id):
    exp = Experiment.query.get_or_404(exp_id)
    state = service.experiment_state(exp)
    planned = exp.num_prompts * exp.samples_per_prompt
    return render_template(
        "results.html",
        results=service.results(exp),
        complete=state["complete"],
        planned=planned,
    )


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
@bp.route("/api/experiments", methods=["POST"])
def create_experiment():
    data = request.get_json(silent=True) or {}
    user_email = (data.get("user_email") or "").strip()
    if user_email not in USERS:
        return jsonify(error="unknown user"), 400
    try:
        num_prompts = int(data.get("num_prompts"))
        samples_per_prompt = int(data.get("samples_per_prompt"))
    except (TypeError, ValueError):
        return jsonify(error="num_prompts and samples_per_prompt must be integers"), 400
    if num_prompts < 1 or samples_per_prompt < 1:
        return jsonify(error="values must be >= 1"), 400
    exp = service.create_experiment(num_prompts, samples_per_prompt, user_email)
    return jsonify(id=exp.id, url=url_for("main.experiment_page", exp_id=exp.id)), 201


@bp.route("/api/experiments/<int:exp_id>/state")
def get_state(exp_id):
    exp = Experiment.query.get_or_404(exp_id)
    return jsonify(service.experiment_state(exp))


@bp.route("/api/experiments/<int:exp_id>/prompts", methods=["POST"])
def add_prompt(exp_id):
    exp = Experiment.query.get_or_404(exp_id)
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify(error="prompt text required"), 400
    if len(list(exp.prompts)) >= exp.num_prompts:
        return jsonify(error="all prompts already entered"), 400
    p = service.add_prompt(exp, text)
    return jsonify(prompt_id=p.id), 201


@bp.route("/api/experiments/<int:exp_id>/generate_all", methods=["POST"])
def generate_all(exp_id):
    exp = Experiment.query.get_or_404(exp_id)
    data = request.get_json(silent=True) or {}
    prompt_id = data.get("prompt_id")
    prompt = db.session.get(Prompt, prompt_id) if prompt_id is not None else None
    if prompt is None or prompt.experiment_id != exp.id:
        return jsonify(error="valid prompt_id required"), 400
    try:
        result = service.generate_all(exp, prompt)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except Exception as exc:  # noqa: BLE001 - provider/network failure
        return jsonify(error="generation failed: %s" % exc), 502
    return jsonify(result)


@bp.route("/api/experiments/<int:exp_id>/sample", methods=["POST"])
def sample(exp_id):
    exp = Experiment.query.get_or_404(exp_id)
    data = request.get_json(silent=True) or {}
    prompt_id = data.get("prompt_id")
    prompt = db.session.get(Prompt, prompt_id) if prompt_id is not None else None
    if prompt is None or prompt.experiment_id != exp.id:
        return jsonify(error="valid prompt_id required"), 400
    try:
        payload = service.sample(exp, prompt)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except Exception as exc:  # noqa: BLE001 - provider/network failure
        return jsonify(error="generation failed: %s" % exc), 502
    return jsonify(payload)


@bp.route("/api/comparisons/<int:comparison_id>/choose", methods=["POST"])
def choose(comparison_id):
    comparison = Comparison.query.get_or_404(comparison_id)
    data = request.get_json(silent=True) or {}
    try:
        winner_slot = int(data.get("winner_slot"))
    except (TypeError, ValueError):
        return jsonify(error="winner_slot must be 1 or 2"), 400
    try:
        service.choose_winner(comparison, winner_slot)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(ok=True)


@bp.route("/api/experiments/<int:exp_id>/results")
def get_results(exp_id):
    exp = Experiment.query.get_or_404(exp_id)
    return jsonify(service.results(exp))


@bp.route("/api/audio/<int:gen_id>")
def audio(gen_id):
    gen = Generation.query.get_or_404(gen_id)
    if not gen.audio_path or not os.path.exists(gen.audio_path):
        abort(404)
    mimetype = _MIME.get(gen.audio_format, "application/octet-stream")
    # No provider info in the response — clips stay anonymous to the listener.
    return send_file(gen.audio_path, mimetype=mimetype, conditional=True)
