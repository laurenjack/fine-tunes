"""HTTP routes: pages + JSON API."""
import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import service
from .config import BASE_DIR, DEFAULT_USER, USERS
from .models import (
    KIND_HEAD_TO_HEAD,
    KIND_ROLLOUT,
    KINDS,
    Comparison,
    Experiment,
    Generation,
    Prompt,
    db,
)
from .providers import PROVIDER_NAMES, display_name, is_mock

router = APIRouter()
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

_STATIC_DIR = os.path.join(BASE_DIR, "static")


def _static_v(path: str) -> str:
    """Return /static/<path>?v=<mtime> so JS/CSS edits bust the browser cache."""
    clean = path.lstrip("/")
    try:
        mtime = int(os.path.getmtime(os.path.join(_STATIC_DIR, clean)))
    except OSError:
        mtime = 0
    return f"/static/{clean}?v={mtime}"


templates.env.globals["static_v"] = _static_v

_MIME = {"mp3": "audio/mpeg", "wav": "audio/wav"}


def _body(data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return data if isinstance(data, dict) else {}


def _error(message, status_code=status.HTTP_400_BAD_REQUEST):
    return JSONResponse({"error": message}, status_code=status_code)


def _get_or_404(model, ident):
    obj = db.session.get(model, ident)
    if obj is None:
        raise HTTPException(status_code=404, detail="not found")
    return obj


def _model_options():
    """List of {name, label} for the model picker(s)."""
    return [{"name": name, "label": display_name(name)} for name in PROVIDER_NAMES]


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
@router.get("/", name="index")
def index():
    return RedirectResponse(url="/experiments", status_code=status.HTTP_302_FOUND)


@router.get("/experiments", name="experiments_overview")
def experiments_overview(request: Request):
    mocking = is_mock("elevenlabs") or is_mock("stable_audio")
    rows = service.experiment_overview_rows()
    return templates.TemplateResponse(
        request,
        "experiments.html",
        {
            "mocking": mocking,
            "users": USERS,
            "default_user": DEFAULT_USER,
            "models": _model_options(),
            "head_to_head_rows": rows["head_to_head"],
            "rollout_rows": rows["rollout"],
        },
    )


@router.get("/experiment/{exp_id}", name="experiment_page")
def experiment_page(request: Request, exp_id: int):
    exp = _get_or_404(Experiment, exp_id)
    return templates.TemplateResponse(request, "experiment.html", {"experiment": exp})


@router.get("/experiment/{exp_id}/results", name="results_page")
def results_page(request: Request, exp_id: int):
    exp = _get_or_404(Experiment, exp_id)
    state = service.experiment_state(exp)
    return templates.TemplateResponse(
        request,
        "results.html",
        {
            "results": service.results(exp),
            "complete": state["complete"],
            "experiment_id": exp.id,
        },
    )


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
@router.get("/api/experiments")
def list_experiments():
    return service.experiment_overview_rows()


@router.post("/api/experiments", status_code=status.HTTP_201_CREATED)
def create_experiment(
    data: Optional[Dict[str, Any]] = Body(default=None),
):
    data = _body(data)
    user_email = (data.get("user_email") or "").strip()
    if user_email not in USERS:
        return _error("unknown user")
    kind = (data.get("kind") or "").strip()
    if kind not in KINDS:
        return _error("kind must be 'head_to_head' or 'rollout'")
    model_a = (data.get("model_a") or "").strip()
    model_b_raw = data.get("model_b")
    model_b = model_b_raw.strip() if isinstance(model_b_raw, str) else None
    if kind == KIND_HEAD_TO_HEAD and not model_b:
        return _error("model_b is required for head-to-head experiments")
    try:
        num_prompts = int(data.get("num_prompts"))
    except (TypeError, ValueError):
        return _error("num_prompts must be an integer")
    try:
        exp = service.create_experiment(user_email, kind, model_a, model_b, num_prompts)
    except ValueError as exc:
        return _error(str(exc))
    return {"id": exp.id, "url": "/experiment/%d" % exp.id}


@router.get("/api/experiments/{exp_id}/state")
def get_state(exp_id: int):
    exp = _get_or_404(Experiment, exp_id)
    return service.experiment_state(exp)


@router.post("/api/experiments/{exp_id}/prompts", status_code=status.HTTP_201_CREATED)
def add_prompt(
    exp_id: int,
    data: Optional[Dict[str, Any]] = Body(default=None),
):
    exp = _get_or_404(Experiment, exp_id)
    data = _body(data)
    text = (data.get("text") or "").strip()
    if not text:
        return _error("prompt text required")
    if len(list(exp.prompts)) >= exp.num_prompts:
        return _error("all prompts already entered")
    prompt = service.add_prompt(exp, text)
    return {"prompt_id": prompt.id}


@router.post("/api/experiments/{exp_id}/generate")
def generate(
    exp_id: int,
    data: Optional[Dict[str, Any]] = Body(default=None),
):
    exp = _get_or_404(Experiment, exp_id)
    data = _body(data)
    prompt_id = data.get("prompt_id")
    prompt = db.session.get(Prompt, prompt_id) if prompt_id is not None else None
    if prompt is None or prompt.experiment_id != exp.id:
        return _error("valid prompt_id required")
    try:
        result = service.generate_candidates(exp, prompt)
    except ValueError as exc:
        return _error(str(exc))
    except Exception as exc:  # noqa: BLE001 - provider/network failure
        return _error("generation failed: %s" % exc, status.HTTP_502_BAD_GATEWAY)
    return result


@router.post("/api/comparisons/{comparison_id}/rank")
def rank(
    comparison_id: int,
    data: Optional[Dict[str, Any]] = Body(default=None),
):
    comparison = _get_or_404(Comparison, comparison_id)
    data = _body(data)
    try:
        service.submit_ranking(comparison, data.get("ranked_slots"))
    except ValueError as exc:
        return _error(str(exc))
    return {"ok": True}


@router.get("/api/experiments/{exp_id}/results")
def get_results(exp_id: int):
    exp = _get_or_404(Experiment, exp_id)
    return service.results(exp)


@router.get("/api/experiments/{exp_id}/preferences")
def export_preferences(exp_id: int):
    exp = _get_or_404(Experiment, exp_id)
    if exp.kind != KIND_ROLLOUT:
        return _error("preference export is only available for rollout experiments")
    return service.preference_pairs(exp)


@router.get("/api/audio/{gen_id}")
def audio(gen_id: int):
    gen = _get_or_404(Generation, gen_id)
    if not gen.audio_path or not os.path.exists(gen.audio_path):
        raise HTTPException(status_code=404, detail="not found")
    media_type = _MIME.get(gen.audio_format, "application/octet-stream")
    # No provider info in the response: clips stay anonymous to the listener.
    return FileResponse(gen.audio_path, media_type=media_type)
