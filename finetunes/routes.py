"""HTTP routes: pages + JSON API."""
import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from . import service
from .config import BASE_DIR, DEFAULT_USER, USERS
from .models import Comparison, Experiment, Generation, Prompt, db
from .providers import is_mock

router = APIRouter()
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

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


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
@router.get("/", name="index")
def index(request: Request):
    mocking = is_mock("elevenlabs") or is_mock("stable_audio")
    experiments = [
        {
            "id": exp.id,
            "user_email": exp.user_email,
            "decided": service.results(exp)["n"],
            "planned": exp.num_prompts * exp.samples_per_prompt,
            "complete": service.experiment_state(exp)["complete"],
        }
        for exp in db.session.query(Experiment).order_by(Experiment.id.desc()).all()
    ]
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "mocking": mocking,
            "users": USERS,
            "default_user": DEFAULT_USER,
            "experiments": experiments,
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
    planned = exp.num_prompts * exp.samples_per_prompt
    return templates.TemplateResponse(
        request,
        "results.html",
        {
            "results": service.results(exp),
            "complete": state["complete"],
            "planned": planned,
        },
    )


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
@router.post("/api/experiments", status_code=status.HTTP_201_CREATED)
def create_experiment(
    data: Optional[Dict[str, Any]] = Body(default=None),
):
    data = _body(data)
    user_email = (data.get("user_email") or "").strip()
    if user_email not in USERS:
        return _error("unknown user")
    try:
        num_prompts = int(data.get("num_prompts"))
        samples_per_prompt = int(data.get("samples_per_prompt"))
    except (TypeError, ValueError):
        return _error("num_prompts and samples_per_prompt must be integers")
    if num_prompts < 1 or samples_per_prompt < 1:
        return _error("values must be >= 1")
    exp = service.create_experiment(num_prompts, samples_per_prompt, user_email)
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


@router.post("/api/experiments/{exp_id}/generate_all")
def generate_all(
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
        result = service.generate_all(exp, prompt)
    except ValueError as exc:
        return _error(str(exc))
    except Exception as exc:  # noqa: BLE001 - provider/network failure
        return _error("generation failed: %s" % exc, status.HTTP_502_BAD_GATEWAY)
    return result


@router.post("/api/experiments/{exp_id}/sample")
def sample(
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
        payload = service.sample(exp, prompt)
    except ValueError as exc:
        return _error(str(exc))
    except Exception as exc:  # noqa: BLE001 - provider/network failure
        return _error("generation failed: %s" % exc, status.HTTP_502_BAD_GATEWAY)
    return payload


@router.post("/api/comparisons/{comparison_id}/choose")
def choose(
    comparison_id: int,
    data: Optional[Dict[str, Any]] = Body(default=None),
):
    comparison = _get_or_404(Comparison, comparison_id)
    data = _body(data)
    try:
        winner_slot = int(data.get("winner_slot"))
    except (TypeError, ValueError):
        return _error("winner_slot must be 1 or 2")
    try:
        service.choose_winner(comparison, winner_slot)
    except ValueError as exc:
        return _error(str(exc))
    return {"ok": True}


@router.get("/api/experiments/{exp_id}/results")
def get_results(exp_id: int):
    exp = _get_or_404(Experiment, exp_id)
    return service.results(exp)


@router.get("/api/audio/{gen_id}")
def audio(gen_id: int):
    gen = _get_or_404(Generation, gen_id)
    if not gen.audio_path or not os.path.exists(gen.audio_path):
        raise HTTPException(status_code=404, detail="not found")
    media_type = _MIME.get(gen.audio_format, "application/octet-stream")
    # No provider info in the response: clips stay anonymous to the listener.
    return FileResponse(gen.audio_path, media_type=media_type)
