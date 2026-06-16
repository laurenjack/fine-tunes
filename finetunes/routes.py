"""HTTP routes: pages + JSON API."""
import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import service
from .config import BASE_DIR, DEFAULT_USER, USERS
from .models import (
    Comparison,
    Experiment,
    Generation,
    Prompt,
    Rollout,
    RolloutCandidate,
    RolloutPrompt,
    db,
)
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
def index():
    return RedirectResponse(url="/experiments", status_code=status.HTTP_302_FOUND)


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


@router.get("/experiments", name="experiments_overview")
def experiments_overview(request: Request):
    mocking = is_mock("elevenlabs") or is_mock("stable_audio")
    return templates.TemplateResponse(
        request,
        "experiments.html",
        {
            "mocking": mocking,
            "users": USERS,
            "default_user": DEFAULT_USER,
            "experiments": service.experiment_overview_rows(),
        },
    )


@router.get("/rollout", name="rollout_index")
def rollout_index():
    return RedirectResponse(url="/rollouts", status_code=status.HTTP_302_FOUND)


@router.get("/rollout/{rollout_id}", name="rollout_page")
def rollout_page(request: Request, rollout_id: int):
    rollout = _get_or_404(Rollout, rollout_id)
    return templates.TemplateResponse(request, "rollout.html", {"rollout": rollout})


@router.get("/rollouts", name="rollouts_overview")
def rollouts_overview(request: Request):
    mocking = is_mock("elevenlabs") or is_mock("stable_audio")
    return templates.TemplateResponse(
        request,
        "rollouts.html",
        {
            "mocking": mocking,
            "users": USERS,
            "default_user": DEFAULT_USER,
            "rollout_summaries": service.rollout_summary_rows(),
            "rollout_rows": service.rollout_overview_rows(),
            "outputs_per_prompt": service.ROLLOUT_OUTPUTS_PER_PROMPT,
        },
    )


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
@router.get("/api/experiments")
def list_experiments():
    return {"experiments": service.experiment_overview_rows()}


@router.get("/api/rollouts")
def list_rollouts():
    return {"rollouts": service.rollout_summary_rows()}


@router.post("/api/rollouts", status_code=status.HTTP_201_CREATED)
def create_rollout(
    data: Optional[Dict[str, Any]] = Body(default=None),
):
    data = _body(data)
    user_email = (data.get("user_email") or "").strip()
    if user_email not in USERS:
        return _error("unknown user")
    try:
        num_prompts = int(data.get("num_prompts"))
    except (TypeError, ValueError):
        return _error("num_prompts must be an integer")
    if num_prompts < 1:
        return _error("num_prompts must be >= 1")
    try:
        rollout = service.create_rollout(num_prompts, user_email)
    except ValueError as exc:
        return _error(str(exc))
    return {"id": rollout.id, "url": "/rollout/%d" % rollout.id}


@router.get("/api/rollouts/rankings")
def list_rollout_rankings():
    return {"rankings": service.rollout_rankings()}


@router.get("/api/rollouts/{rollout_id}/state")
def get_rollout_state(rollout_id: int):
    rollout = _get_or_404(Rollout, rollout_id)
    return service.rollout_state(rollout)


@router.post("/api/rollouts/{rollout_id}/prompts", status_code=status.HTTP_201_CREATED)
def add_rollout_prompt(
    rollout_id: int,
    data: Optional[Dict[str, Any]] = Body(default=None),
):
    rollout = _get_or_404(Rollout, rollout_id)
    data = _body(data)
    text = (data.get("text") or "").strip()
    if not text:
        return _error("prompt text required")
    if len(list(rollout.prompts)) >= rollout.num_prompts:
        return _error("all prompts already entered")
    prompt = service.add_rollout_prompt(rollout, text)
    return {"prompt_id": prompt.id}


@router.post("/api/rollouts/{rollout_id}/generate")
def generate_rollout(
    rollout_id: int,
    data: Optional[Dict[str, Any]] = Body(default=None),
):
    rollout = _get_or_404(Rollout, rollout_id)
    data = _body(data)
    prompt_id = data.get("prompt_id")
    prompt = db.session.get(RolloutPrompt, prompt_id) if prompt_id is not None else None
    if prompt is None or prompt.rollout_id != rollout.id:
        return _error("valid prompt_id required")
    try:
        result = service.generate_rollout_candidates(rollout, prompt)
    except ValueError as exc:
        return _error(str(exc))
    except Exception as exc:  # noqa: BLE001 - provider/network failure
        return _error("generation failed: %s" % exc, status.HTTP_502_BAD_GATEWAY)
    return result


@router.post("/api/rollout-prompts/{prompt_id}/rank")
def rank_rollout_prompt(
    prompt_id: int,
    data: Optional[Dict[str, Any]] = Body(default=None),
):
    prompt = _get_or_404(RolloutPrompt, prompt_id)
    data = _body(data)
    try:
        ranking = service.rank_rollout_prompt(prompt, data.get("ranked_slots"))
    except ValueError as exc:
        return _error(str(exc))
    return {"ok": True, "ranking": ranking}


@router.get("/api/rollouts/{rollout_id}/rankings")
def get_rollout_rankings(rollout_id: int):
    rollout = _get_or_404(Rollout, rollout_id)
    return {"rankings": service.rollout_rankings(rollout)}


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


@router.get("/api/rollout-candidates/{candidate_id}/audio")
def rollout_audio(candidate_id: int):
    candidate = _get_or_404(RolloutCandidate, candidate_id)
    if not candidate.audio_path or not os.path.exists(candidate.audio_path):
        raise HTTPException(status_code=404, detail="not found")
    media_type = _MIME.get(candidate.audio_format, "application/octet-stream")
    return FileResponse(candidate.audio_path, media_type=media_type)
