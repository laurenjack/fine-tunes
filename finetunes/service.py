"""Experiment flow logic: state derivation, generation, ranking, scoring."""
import json
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests

from .models import (
    CANDIDATES_PER_PROMPT,
    HALF_CANDIDATES,
    KIND_HEAD_TO_HEAD,
    KIND_ROLLOUT,
    KINDS,
    Comparison,
    Experiment,
    Generation,
    Prompt,
    db,
)
from .providers import PROVIDER_NAMES, display_name, get_provider
from .stats import crosspair_wins, wilson_interval

_PROMPT_PREVIEW_CHARS = 80

# Max concurrent in-flight requests per provider during batch generation.
# ElevenLabs Music enforces a per-tier concurrency cap (Free 2, Starter 3,
# Creator 5, Pro 10, Scale/Business 15) and returns 429 too_many_concurrent_
# _requests beyond it - so we queue rather than exceed it. Defaults are
# Free-safe; override via env for a higher tier.
_MAX_CONCURRENCY = {
    "elevenlabs": int(os.environ.get("ELEVENLABS_MAX_CONCURRENCY", "2")),
    "stable_audio": int(os.environ.get("FAL_MAX_CONCURRENCY", "4")),
}
# HTTP statuses worth retrying with backoff (transient: system_busy / 5xx).
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# Serialise generation per prompt: concurrent requests (e.g. page reloads)
# must not duplicate work or collectively exceed a provider's concurrency cap.
_prompt_locks = {}
_prompt_locks_guard = threading.Lock()


def _prompt_lock(prompt_id):
    with _prompt_locks_guard:
        lock = _prompt_locks.get(prompt_id)
        if lock is None:
            lock = threading.Lock()
            _prompt_locks[prompt_id] = lock
        return lock


def _config():
    if db.config is None:
        raise RuntimeError("Application config has not been initialised")
    return db.config


def _percent_label(value):
    if value is None:
        return "--"
    return "%.1f%%" % (value * 100)


def _truncate(text, max_chars=_PROMPT_PREVIEW_CHARS):
    normalised = " ".join((text or "").split())
    if len(normalised) <= max_chars:
        return normalised
    return normalised[: max_chars - 3].rstrip() + "..."


def _utcnow():
    return datetime.now(timezone.utc)


def _json_loads(value):
    try:
        return json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}


def _generate_with_retry(provider_name, prompt_text, clip_seconds, attempts=4):
    """Call a provider, retrying transient failures (429/5xx/timeouts) with backoff."""
    delay = 2.0
    for attempt in range(attempts):
        provider = get_provider(provider_name)
        try:
            return provider.generate(prompt_text, clip_seconds)
        except requests.HTTPError as exc:
            status = getattr(exc.response, "status_code", None)
            if status in _RETRYABLE_STATUS and attempt < attempts - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise
        except requests.RequestException:  # timeouts, connection errors
            if attempt < attempts - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise


# --------------------------------------------------------------------------- #
# Experiment lifecycle
# --------------------------------------------------------------------------- #
def create_experiment(user_email, kind, model_a, model_b, num_prompts):
    if kind not in KINDS:
        raise ValueError("kind must be one of %s" % (KINDS,))
    if model_a not in PROVIDER_NAMES:
        raise ValueError("unknown model_a: %s" % model_a)
    if kind == KIND_ROLLOUT:
        model_b = None
    else:
        if model_b not in PROVIDER_NAMES:
            raise ValueError("unknown model_b: %s" % model_b)
    if num_prompts < 1:
        raise ValueError("num_prompts must be >= 1")
    exp = Experiment(
        user_email=user_email,
        kind=kind,
        model_a=model_a,
        model_b=model_b,
        num_prompts=num_prompts,
        candidates_per_prompt=CANDIDATES_PER_PROMPT,
        clip_seconds=_config().CLIP_SECONDS,
    )
    db.session.add(exp)
    db.session.commit()
    return exp


def add_prompt(exp, text):
    order_index = len(list(exp.prompts))
    p = Prompt(experiment_id=exp.id, order_index=order_index, text=text)
    db.session.add(p)
    db.session.commit()
    return p


def _comparison_for(prompt):
    """The one ranking session per prompt (created lazily)."""
    comps = list(prompt.comparisons)
    return comps[0] if comps else None


def _prompt_fully_generated(prompt, candidates_per_prompt):
    comp = _comparison_for(prompt)
    if comp is None:
        return False
    gens = list(comp.generations)
    if len(gens) < candidates_per_prompt:
        return False
    return all(g.status == "ok" for g in gens)


def _ranked_count(exp):
    return sum(
        1
        for p in exp.prompts
        for c in p.comparisons
        if c.is_ranked
    )


def _comparison_payload(comparison):
    """Anonymised view for the frontend: just slots + audio URLs, no provider."""
    songs = []
    for g in sorted(comparison.generations, key=lambda g: g.slot):
        if g.status != "ok":
            continue
        songs.append(
            {
                "slot": g.slot,
                "url": "/api/audio/%d" % g.id,
                "format": g.audio_format,
            }
        )
    return {"comparison_id": comparison.id, "songs": songs}


def experiment_state(exp):
    """Derive the current step of the experiment from what's in the DB."""
    prompts = list(exp.prompts)

    current = None
    for p in prompts:
        comp = _comparison_for(p)
        if comp is None or not comp.is_ranked:
            current = p
            break

    complete = len(prompts) == exp.num_prompts and current is None

    state = {
        "experiment_id": exp.id,
        "user_email": exp.user_email,
        "kind": exp.kind,
        "model_a": exp.model_a,
        "model_a_name": display_name(exp.model_a),
        "model_b": exp.model_b,
        "model_b_name": display_name(exp.model_b) if exp.model_b else None,
        "num_prompts": exp.num_prompts,
        "candidates_per_prompt": exp.candidates_per_prompt,
        "clip_seconds": exp.clip_seconds,
        "prompts_created": len(prompts),
        "complete": complete,
        "need_new_prompt": current is None and not complete,
        "current_prompt": None,
        "open_comparison": None,
        "prompt_position": None,
    }

    if current is not None:
        needs_generation = not _prompt_fully_generated(current, exp.candidates_per_prompt)
        state["current_prompt"] = {
            "id": current.id,
            "text": current.text,
            "needs_generation": needs_generation,
        }
        state["prompt_position"] = current.order_index + 1
        comp = _comparison_for(current)
        if comp is not None and not needs_generation:
            state["open_comparison"] = _comparison_payload(comp)
    elif not complete:
        state["prompt_position"] = len(prompts) + 1

    return state


# --------------------------------------------------------------------------- #
# Generation
# --------------------------------------------------------------------------- #
def _storage_dir(experiment_id):
    base = _config().AUDIO_STORAGE_DIR
    d = os.path.join(base, str(experiment_id))
    os.makedirs(d, exist_ok=True)
    return d


def _plan_slots(exp):
    """Decide which (side, slot) goes to which model for a fresh comparison.

    Returns a list of (slot, provider, side) tuples, length == candidates_per_prompt.

    head_to_head (model_a != model_b): shuffle slots so the model behind each
    is hidden from the listener.
    head_to_head (model_a == model_b): deterministic - side 'a' (model_a) goes
    to slots 1..HALF, side 'b' (model_b) to slots HALF+1..N. The win rate then
    measures whether the listener favours the first half (position bias).
    rollout: all six from model_a, side 'a'; slot order shuffled (cosmetic).
    """
    n = exp.candidates_per_prompt
    if exp.kind == KIND_ROLLOUT:
        slots = list(range(1, n + 1))
        random.shuffle(slots)
        return [(slot, exp.model_a, "a") for slot in slots]

    # head_to_head: half from a, half from b.
    a_count = HALF_CANDIDATES
    b_count = n - HALF_CANDIDATES
    if exp.is_same_model:
        a_slots = list(range(1, a_count + 1))
        b_slots = list(range(a_count + 1, n + 1))
    else:
        all_slots = list(range(1, n + 1))
        random.shuffle(all_slots)
        a_slots = all_slots[:a_count]
        b_slots = all_slots[a_count:]
    plan = [(slot, exp.model_a, "a") for slot in a_slots]
    plan += [(slot, exp.model_b, "b") for slot in b_slots]
    return plan


def generate_candidates(exp, prompt):
    """Create + generate all candidates for one prompt.

    Idempotent / self-healing: creates the Comparison + missing Generations
    only if not already present, and (re)generates any clip not yet 'ok'. The
    provider calls run in parallel; DB writes stay on this thread.
    """
    if prompt.experiment_id != exp.id:
        raise ValueError("Prompt does not belong to this experiment")

    with _prompt_lock(prompt.id):
        comparison = _comparison_for(prompt)
        if comparison is None:
            comparison = Comparison(experiment_id=exp.id, prompt_id=prompt.id)
            db.session.add(comparison)
            db.session.flush()
            for slot, provider_name, side in _plan_slots(exp):
                db.session.add(
                    Generation(
                        comparison_id=comparison.id,
                        slot=slot,
                        provider=provider_name,
                        side=side,
                        prompt_text=prompt.text,
                        request_payload="{}",
                        status="pending",
                    )
                )
            db.session.commit()

        todo = [g for g in comparison.generations if g.status != "ok"]
        if not todo:
            return {
                "candidates": exp.candidates_per_prompt,
                "generated": 0,
                "failed": 0,
            }

        storage_dir = _storage_dir(exp.id)
        prompt_text = prompt.text
        clip_seconds = exp.clip_seconds

        sems = {
            name: threading.Semaphore(_MAX_CONCURRENCY.get(name, 2))
            for name in PROVIDER_NAMES
        }

        def _work(gen_id, provider_name):
            with sems[provider_name]:
                return gen_id, _generate_with_retry(provider_name, prompt_text, clip_seconds)

        generated = 0
        failed = 0
        with ThreadPoolExecutor(max_workers=min(8, len(todo))) as ex:
            futures = {ex.submit(_work, g.id, g.provider): g.id for g in todo}
            for fut in as_completed(futures):
                gen = db.session.get(Generation, futures[fut])
                try:
                    _, result = fut.result()
                except Exception as exc:  # noqa: BLE001 - record and keep going
                    gen.status = "error"
                    gen.error = repr(exc)
                    failed += 1
                    db.session.commit()
                    continue
                path = os.path.join(
                    storage_dir, "gen_%d.%s" % (gen.id, result.audio_format)
                )
                with open(path, "wb") as f:
                    f.write(result.audio_bytes)
                gen.audio_path = path
                gen.audio_format = result.audio_format
                gen.duration_ms = result.duration_ms
                gen.request_payload = json.dumps(result.request_payload)
                gen.status = "ok"
                gen.error = None
                generated += 1
                db.session.commit()  # persist each clip as it lands

    if failed:
        raise RuntimeError("%d of %d clips failed to generate" % (failed, len(todo)))
    return {
        "candidates": exp.candidates_per_prompt,
        "generated": generated,
        "failed": 0,
    }


# --------------------------------------------------------------------------- #
# Ranking
# --------------------------------------------------------------------------- #
def submit_ranking(comparison, ranked_slots):
    """Record a full ranking of the comparison's candidates.

    `ranked_slots` is a list of slot numbers in user-preferred order: the slot
    at index 0 becomes rank 1 (best); the last is rank N (worst). Must be a
    permutation of slots 1..N.
    """
    if comparison.is_ranked:
        return  # idempotent: already submitted
    if not isinstance(ranked_slots, list):
        raise ValueError("ranked_slots must be a list")
    try:
        slots = [int(s) for s in ranked_slots]
    except (TypeError, ValueError):
        raise ValueError("ranked_slots must contain slot numbers")

    gens = list(comparison.generations)
    n = len(gens)
    expected = set(range(1, n + 1))
    if len(slots) != n or set(slots) != expected:
        raise ValueError("ranked_slots must contain each slot exactly once")
    if any(g.status != "ok" for g in gens):
        raise ValueError("all candidates must be generated before ranking")

    by_slot = {g.slot: g for g in gens}
    for rank_position, slot in enumerate(slots, start=1):
        by_slot[slot].rank_position = rank_position
    comparison.ranked_at = _utcnow()
    db.session.commit()


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
def _ranked_comparisons(exp):
    out = []
    for p in exp.prompts:
        comp = _comparison_for(p)
        if comp is not None and comp.is_ranked:
            out.append(comp)
    return out


def results(exp):
    """Aggregate scoring for an experiment.

    Returns a dict shaped for the results page. The fields differ slightly by
    kind, but `kind`, `n_prompts_ranked`, and `experiment_id` are always set.
    """
    ranked = _ranked_comparisons(exp)
    n_prompts = len(ranked)
    base = {
        "experiment_id": exp.id,
        "kind": exp.kind,
        "n_prompts_ranked": n_prompts,
        "candidates_per_prompt": exp.candidates_per_prompt,
        "model_a": exp.model_a,
        "model_a_name": display_name(exp.model_a),
        "model_b": exp.model_b,
        "model_b_name": display_name(exp.model_b) if exp.model_b else None,
        "same_model": exp.is_same_model,
    }

    if exp.kind == KIND_ROLLOUT:
        # Each ranked prompt yields C(N,2) ordered preference pairs for export.
        n = exp.candidates_per_prompt
        pairs_per_prompt = n * (n - 1) // 2
        base.update(
            {
                "preference_pairs": n_prompts * pairs_per_prompt,
                "pairs_per_prompt": pairs_per_prompt,
                "export_url": "/api/experiments/%d/preferences" % exp.id,
            }
        )
        return base

    # head_to_head: cross-pair win rate (Mann-Whitney U) with Wilson 95% CI.
    a_wins = 0.0
    total = 0
    for comp in ranked:
        ranks_a, ranks_b = [], []
        for g in comp.generations:
            if g.rank_position is None:
                continue
            (ranks_a if g.side == "a" else ranks_b).append(g.rank_position)
        wins, n_pairs = crosspair_wins(ranks_a, ranks_b)
        a_wins += wins
        total += n_pairs

    point, low, high = wilson_interval(a_wins, total)
    base.update(
        {
            "a_wins": a_wins,
            "b_wins": total - a_wins,
            "n_pairs": total,
            "win_rate": point,
            "ci_low": low,
            "ci_high": high,
            "confidence": 0.95,
        }
    )
    return base


def _winner_summary(res):
    """Compact summary for the experiments-overview table."""
    if res["kind"] == KIND_ROLLOUT:
        return {
            "ratio_label": "%d / %d ranked" % (res["n_prompts_ranked"], res.get("num_prompts", 0)),
            "winner": "--",
            "confidence_label": "--",
        }
    if res["n_pairs"] == 0:
        return {"winner": "--", "confidence_label": "--"}
    midpoint = 0.5
    if res["win_rate"] > midpoint:
        if res["same_model"]:
            winner = "Slots 1-%d" % HALF_CANDIDATES
        else:
            winner = res["model_a_name"]
        confidence = res["ci_low"]
    elif res["win_rate"] < midpoint:
        if res["same_model"]:
            winner = "Slots %d-%d" % (HALF_CANDIDATES + 1, res["candidates_per_prompt"])
        else:
            winner = res["model_b_name"]
        confidence = 1.0 - res["ci_high"]
    else:
        winner = "Tie"
        confidence = None
    return {"winner": winner, "confidence_label": _percent_label(confidence)}


def experiment_overview(exp):
    res = results(exp)
    prompts = list(exp.prompts)
    prompt_texts = [" ".join(p.text.split()) for p in prompts if p.text.strip()]
    if prompt_texts:
        prompt_full = " | ".join(prompt_texts)
        prompt = _truncate(prompt_texts[0])
        if len(prompt_texts) > 1:
            prompt = _truncate("%s (+%d more)" % (prompt_texts[0], len(prompt_texts) - 1))
    else:
        prompt_full = "No prompt yet"
        prompt = prompt_full

    if exp.kind == KIND_ROLLOUT:
        winner = "--"
        confidence_label = "--"
        model_a_label = display_name(exp.model_a)
        model_b_label = "--"
        a_count = res["n_prompts_ranked"]
        b_count = 0
        a_header = "Ranked"
        b_header = ""
    else:
        # H2H header rows: counts of cross-pair wins
        a_count = int(round(res["a_wins"]))
        b_count = int(round(res["b_wins"]))
        if exp.is_same_model:
            model_a_label = "Slots 1-%d" % HALF_CANDIDATES
            model_b_label = "Slots %d-%d" % (HALF_CANDIDATES + 1, res["candidates_per_prompt"])
        else:
            model_a_label = display_name(exp.model_a)
            model_b_label = display_name(exp.model_b)
        summary = _winner_summary({**res, "num_prompts": exp.num_prompts})
        winner = summary["winner"]
        confidence_label = summary["confidence_label"]
        a_header = "Side A wins"
        b_header = "Side B wins"

    return {
        "id": exp.id,
        "url": "/experiment/%d" % exp.id,
        "kind": exp.kind,
        "kind_label": "Head-to-head" if exp.kind == KIND_HEAD_TO_HEAD else "Rollout",
        "prompt": prompt,
        "prompt_full": prompt_full,
        "model_a_name": model_a_label,
        "model_a_votes": a_count,
        "model_b_name": model_b_label,
        "model_b_votes": b_count,
        "confidence_score_label": confidence_label,
        "winning_model_name": winner,
        "a_header": a_header,
        "b_header": b_header,
    }


def experiment_overview_rows():
    experiments = db.session.query(Experiment).order_by(Experiment.id.desc()).all()
    return {
        "head_to_head": [experiment_overview(e) for e in experiments if e.kind == KIND_HEAD_TO_HEAD],
        "rollout": [experiment_overview(e) for e in experiments if e.kind == KIND_ROLLOUT],
    }


# --------------------------------------------------------------------------- #
# Rollout preference-pair export
# --------------------------------------------------------------------------- #
def preference_pairs(exp):
    """Export ranked rollout prompts as preference pairs for RL training.

    For each ranked prompt with candidates c1..cN ranked 1..N (1=best), emit
    every ordered pair (i, j) with rank_i < rank_j as a {preferred, rejected}
    object. Each prompt contributes C(N,2) pairs.
    """
    if exp.kind != KIND_ROLLOUT:
        raise ValueError("preference_pairs is only defined for rollout experiments")
    out_prompts = []
    for prompt in exp.prompts:
        comp = _comparison_for(prompt)
        if comp is None or not comp.is_ranked:
            continue
        ranked = sorted(
            comp.generations, key=lambda g: g.rank_position or 1_000_000
        )
        gen_records = [
            {
                "generation_id": g.id,
                "rank": g.rank_position,
                "audio_url": "/api/audio/%d" % g.id,
                "audio_format": g.audio_format,
                "request_payload": _json_loads(g.request_payload),
            }
            for g in ranked
        ]
        pairs = []
        for i in range(len(ranked)):
            for j in range(i + 1, len(ranked)):
                pairs.append({"preferred": gen_records[i], "rejected": gen_records[j]})
        out_prompts.append(
            {
                "prompt_id": prompt.id,
                "prompt_text": prompt.text,
                "ranked_at": comp.ranked_at.isoformat() if comp.ranked_at else None,
                "candidates": gen_records,
                "preference_pairs": pairs,
            }
        )
    return {
        "experiment_id": exp.id,
        "kind": exp.kind,
        "model": exp.model_a,
        "model_name": display_name(exp.model_a),
        "user_email": exp.user_email,
        "rank_semantics": "1 is best",
        "prompts": out_prompts,
    }
