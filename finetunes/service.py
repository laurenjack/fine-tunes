"""Experiment flow logic: state derivation, sampling, scoring."""
import json
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests

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
from .providers import PROVIDER_NAMES, display_name, get_provider
from .stats import wilson_interval

# The provider we report the win rate *for*. The other provider's rate is 1 - x
# (excluding any undecided comparisons).
PRIMARY_PROVIDER = "elevenlabs"
ROLLOUT_OUTPUTS_PER_PROMPT = 6
_PROMPT_PREVIEW_CHARS = 80

# Max concurrent in-flight requests per provider during batch generation.
# ElevenLabs Music enforces a per-tier concurrency cap (Free 2, Starter 3,
# Creator 5, Pro 10, Scale/Business 15) and returns 429 too_many_concurrent_
# _requests beyond it — so we queue rather than exceed it. Defaults are
# Free-safe; override via env for a higher tier.
_MAX_CONCURRENCY = {
    "elevenlabs": int(os.environ.get("ELEVENLABS_MAX_CONCURRENCY", "2")),
    "stable_audio": int(os.environ.get("FAL_MAX_CONCURRENCY", "4")),
}
# HTTP statuses worth retrying with backoff (transient: system_busy / 5xx).
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# Serialise generate_all per prompt: concurrent requests (e.g. page reloads)
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


def _rollout_provider_name():
    provider_name = getattr(_config(), "ROLLOUT_PROVIDER", PRIMARY_PROVIDER).strip()
    provider_name = provider_name or PRIMARY_PROVIDER
    if provider_name not in PROVIDER_NAMES:
        raise ValueError("unknown rollout provider: %s" % provider_name)
    return provider_name


def _rollout_policy_name(provider_name):
    name = getattr(_config(), "ROLLOUT_POLICY_NAME", "").strip()
    return name or display_name(provider_name)


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


def create_experiment(num_prompts, samples_per_prompt, user_email):
    clip_seconds = _config().CLIP_SECONDS
    exp = Experiment(
        user_email=user_email,
        num_prompts=num_prompts,
        samples_per_prompt=samples_per_prompt,
        clip_seconds=clip_seconds,
    )
    db.session.add(exp)
    db.session.commit()
    return exp


def _decided_count(prompt):
    return sum(1 for c in prompt.comparisons if c.is_decided)


def _open_comparison(prompt):
    """An already-generated but undecided comparison, if any (resume after reload)."""
    for c in prompt.comparisons:
        if not c.is_decided and c.generations:
            return c
    return None


def _prompt_fully_generated(prompt, samples_per_prompt):
    """True when every sample pair exists and all its clips generated OK."""
    comps = list(prompt.comparisons)
    if len(comps) < samples_per_prompt:
        return False
    for c in comps:
        if len(c.generations) < len(PROVIDER_NAMES):
            return False
        if any(g.status != "ok" for g in c.generations):
            return False
    return True


def experiment_state(exp):
    """Derive the current step of the experiment from what's in the DB."""
    samples_per_prompt = exp.samples_per_prompt
    prompts = list(exp.prompts)

    current = None
    for p in prompts:
        if _decided_count(p) < samples_per_prompt:
            current = p
            break

    complete = (
        len(prompts) == exp.num_prompts
        and current is None
    )

    state = {
        "experiment_id": exp.id,
        "user_email": exp.user_email,
        "num_prompts": exp.num_prompts,
        "samples_per_prompt": samples_per_prompt,
        "clip_seconds": exp.clip_seconds,
        "prompts_created": len(prompts),
        "complete": complete,
        "need_new_prompt": current is None and not complete,
        "current_prompt": None,
        "open_comparison": None,
        # 1-based index of the prompt the listener is on, for display.
        "prompt_position": None,
    }

    if current is not None:
        decided = _decided_count(current)
        state["current_prompt"] = {
            "id": current.id,
            "text": current.text,
            "samples_done": decided,
            "needs_generation": not _prompt_fully_generated(current, samples_per_prompt),
        }
        state["prompt_position"] = current.order_index + 1
        open_c = _open_comparison(current)
        if open_c is not None:
            state["open_comparison"] = _comparison_payload(open_c)
    elif not complete:
        # Next prompt to be entered.
        state["prompt_position"] = len(prompts) + 1

    return state


def add_prompt(exp, text):
    order_index = len(list(exp.prompts))
    p = Prompt(experiment_id=exp.id, order_index=order_index, text=text)
    db.session.add(p)
    db.session.commit()
    return p


def _storage_dir(experiment_id):
    base = _config().AUDIO_STORAGE_DIR
    d = os.path.join(base, str(experiment_id))
    os.makedirs(d, exist_ok=True)
    return d


def _rollout_storage_dir(rollout_id):
    base = _config().AUDIO_STORAGE_DIR
    d = os.path.join(base, "rollouts", str(rollout_id))
    os.makedirs(d, exist_ok=True)
    return d


def _comparison_payload(comparison):
    """Anonymised view for the frontend: just slots + audio URLs, no provider."""
    songs = []
    for g in sorted(comparison.generations, key=lambda g: g.slot):
        songs.append(
            {
                "slot": g.slot,
                "url": "/api/audio/%d" % g.id,
                "format": g.audio_format,
            }
        )
    return {"comparison_id": comparison.id, "songs": songs}


def sample(exp, prompt):
    """Generate one comparison (a pair of clips) for `prompt`.

    Idempotent-ish: if an undecided comparison already exists for this prompt
    (e.g. the page was reloaded mid-trial), return it instead of regenerating.
    """
    if prompt.experiment_id != exp.id:
        raise ValueError("Prompt does not belong to this experiment")

    existing = _open_comparison(prompt)
    if existing is not None:
        return _comparison_payload(existing)

    if _decided_count(prompt) >= exp.samples_per_prompt:
        raise ValueError("This prompt already has all its samples")

    sample_index = len(list(prompt.comparisons))
    comparison = Comparison(
        experiment_id=exp.id, prompt_id=prompt.id, sample_index=sample_index
    )
    db.session.add(comparison)
    db.session.flush()  # get comparison.id

    # Randomise which provider lands in slot 1 vs slot 2.
    order = list(PROVIDER_NAMES)
    random.shuffle(order)

    storage_dir = _storage_dir(exp.id)
    for slot, provider_name in enumerate(order, start=1):
        gen = Generation(
            comparison_id=comparison.id,
            slot=slot,
            provider=provider_name,
            prompt_text=prompt.text,
            request_payload="{}",
            status="pending",
        )
        db.session.add(gen)
        db.session.flush()  # get gen.id for the filename

        provider = get_provider(provider_name)
        try:
            result = provider.generate(prompt.text, exp.clip_seconds)
        except Exception as exc:  # noqa: BLE001 - surface provider failures
            gen.status = "error"
            gen.error = repr(exc)
            db.session.commit()
            raise

        filename = "gen_%d.%s" % (gen.id, result.audio_format)
        path = os.path.join(storage_dir, filename)
        with open(path, "wb") as f:
            f.write(result.audio_bytes)

        gen.audio_path = path
        gen.audio_format = result.audio_format
        gen.duration_ms = result.duration_ms
        gen.request_payload = json.dumps(result.request_payload)
        gen.status = "ok"

    db.session.commit()
    return _comparison_payload(comparison)


def generate_all(exp, prompt):
    """Create and generate every sample pair for `prompt` up front.

    Idempotent / self-healing: creates only the comparisons still missing and
    (re)generates any clip not yet "ok". The provider calls (network-bound) run
    in parallel; all DB writes stay on this thread.
    """
    if prompt.experiment_id != exp.id:
        raise ValueError("Prompt does not belong to this experiment")

    # Only one generation pass per prompt at a time. A second concurrent request
    # (e.g. a page reload) blocks here, then finds nothing left to do.
    with _prompt_lock(prompt.id):
        existing = list(prompt.comparisons)
        needed = exp.samples_per_prompt - len(existing)
        for i in range(max(0, needed)):
            comparison = Comparison(
                experiment_id=exp.id,
                prompt_id=prompt.id,
                sample_index=len(existing) + i,
            )
            db.session.add(comparison)
            db.session.flush()
            # Randomise which provider lands in slot 1 vs slot 2, per pair.
            order = list(PROVIDER_NAMES)
            random.shuffle(order)
            for slot, provider_name in enumerate(order, start=1):
                db.session.add(
                    Generation(
                        comparison_id=comparison.id,
                        slot=slot,
                        provider=provider_name,
                        prompt_text=prompt.text,
                        request_payload="{}",
                        status="pending",
                    )
                )
        db.session.commit()

        todo = [g for c in prompt.comparisons for g in c.generations if g.status != "ok"]
        if not todo:
            return {"samples": exp.samples_per_prompt, "generated": 0, "failed": 0}

        storage_dir = _storage_dir(exp.id)
        prompt_text = prompt.text
        clip_seconds = exp.clip_seconds
        tasks = [(g.id, g.provider) for g in todo]

        # One semaphore per provider so we never exceed its concurrency cap
        # (ElevenLabs 429s beyond its per-tier limit).
        sems = {name: threading.Semaphore(_MAX_CONCURRENCY.get(name, 2)) for name in PROVIDER_NAMES}

        def _work(gen_id, provider_name):
            # Network only — no DB or app context touched off the main thread.
            with sems[provider_name]:
                return gen_id, _generate_with_retry(provider_name, prompt_text, clip_seconds)

        generated = 0
        failed = 0
        with ThreadPoolExecutor(max_workers=min(8, len(tasks))) as ex:
            futures = {ex.submit(_work, gid, pn): gid for gid, pn in tasks}
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
                path = os.path.join(storage_dir, "gen_%d.%s" % (gen.id, result.audio_format))
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
        # Successful clips are saved; a retry regenerates only the failures.
        raise RuntimeError("%d of %d clips failed to generate" % (failed, len(tasks)))
    return {"samples": exp.samples_per_prompt, "generated": generated, "failed": 0}


def choose_winner(comparison, winner_slot):
    if winner_slot not in (1, 2):
        raise ValueError("winner_slot must be 1 or 2")
    if comparison.is_decided:
        return  # already chosen; ignore double-submit
    gen = next(
        (g for g in comparison.generations if g.slot == winner_slot), None
    )
    if gen is None:
        raise ValueError("No generation in slot %d" % winner_slot)
    comparison.winner_slot = winner_slot
    comparison.winner_provider = gen.provider
    comparison.decided_at = _utcnow()
    db.session.commit()


def results(exp):
    """Overall win rate for PRIMARY_PROVIDER with a 95% Wilson CI."""
    decided = [
        c
        for p in exp.prompts
        for c in p.comparisons
        if c.is_decided
    ]
    n = len(decided)
    primary_wins = sum(1 for c in decided if c.winner_provider == PRIMARY_PROVIDER)
    other_name = next(p for p in PROVIDER_NAMES if p != PRIMARY_PROVIDER)
    other_wins = n - primary_wins

    point, low, high = wilson_interval(primary_wins, n)
    return {
        "experiment_id": exp.id,
        "n": n,
        "primary_provider": PRIMARY_PROVIDER,
        "primary_provider_name": display_name(PRIMARY_PROVIDER),
        "other_provider": other_name,
        "other_provider_name": display_name(other_name),
        "primary_wins": primary_wins,
        "other_wins": other_wins,
        "win_rate": point,
        "ci_low": low,
        "ci_high": high,
        "confidence": 0.95,
    }


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

    model_a_votes = res["primary_wins"]
    model_b_votes = res["other_wins"]
    if model_a_votes > model_b_votes:
        winning_model_name = res["primary_provider_name"]
        confidence_score = res["ci_low"]
    elif model_b_votes > model_a_votes:
        winning_model_name = res["other_provider_name"]
        confidence_score = 1.0 - res["ci_high"]
    elif res["n"] > 0:
        winning_model_name = "Tie"
        confidence_score = None
    else:
        winning_model_name = "--"
        confidence_score = None

    return {
        "id": exp.id,
        "url": "/experiment/%d" % exp.id,
        "prompt": prompt,
        "prompt_full": prompt_full,
        "model_a_name": res["primary_provider_name"],
        "model_a_votes": model_a_votes,
        "model_b_name": res["other_provider_name"],
        "model_b_votes": model_b_votes,
        "confidence_score": confidence_score,
        "confidence_score_label": _percent_label(confidence_score),
        "winning_model_name": winning_model_name,
    }


def experiment_overview_rows():
    experiments = db.session.query(Experiment).order_by(Experiment.id.desc()).all()
    return [experiment_overview(exp) for exp in experiments]


# --------------------------------------------------------------------------- #
# Rollout flow
# --------------------------------------------------------------------------- #
def create_rollout(num_prompts, user_email):
    provider_name = _rollout_provider_name()
    rollout = Rollout(
        user_email=user_email,
        num_prompts=num_prompts,
        outputs_per_prompt=ROLLOUT_OUTPUTS_PER_PROMPT,
        clip_seconds=_config().CLIP_SECONDS,
        provider=provider_name,
        policy_name=_rollout_policy_name(provider_name),
    )
    db.session.add(rollout)
    db.session.commit()
    return rollout


def _ranked_prompt_count(rollout):
    return sum(1 for p in rollout.prompts if p.is_ranked)


def _rollout_prompt_fully_generated(prompt, outputs_per_prompt):
    candidates = list(prompt.candidates)
    if len(candidates) < outputs_per_prompt:
        return False
    return all(c.status == "ok" for c in candidates)


def _rollout_candidate_payload(candidate):
    return {
        "candidate_id": candidate.id,
        "slot": candidate.slot,
        "url": "/api/rollout-candidates/%d/audio" % candidate.id,
        "format": candidate.audio_format,
        "rank_position": candidate.rank_position,
    }


def _rollout_prompt_payload(prompt):
    return {
        "prompt_id": prompt.id,
        "candidates": [
            _rollout_candidate_payload(c)
            for c in sorted(prompt.candidates, key=lambda c: c.slot)
            if c.status == "ok"
        ],
    }


def rollout_state(rollout):
    prompts = list(rollout.prompts)

    current = None
    for prompt in prompts:
        if not prompt.is_ranked:
            current = prompt
            break

    complete = len(prompts) == rollout.num_prompts and current is None
    state = {
        "rollout_id": rollout.id,
        "user_email": rollout.user_email,
        "num_prompts": rollout.num_prompts,
        "outputs_per_prompt": rollout.outputs_per_prompt,
        "clip_seconds": rollout.clip_seconds,
        "provider": rollout.provider,
        "policy_name": rollout.policy_name,
        "prompts_created": len(prompts),
        "ranked_prompts": _ranked_prompt_count(rollout),
        "complete": complete,
        "need_new_prompt": current is None and not complete,
        "current_prompt": None,
        "prompt_position": None,
    }

    if current is not None:
        needs_generation = not _rollout_prompt_fully_generated(
            current, rollout.outputs_per_prompt
        )
        state["current_prompt"] = {
            "id": current.id,
            "text": current.text,
            "needs_generation": needs_generation,
            "candidates": []
            if needs_generation
            else _rollout_prompt_payload(current)["candidates"],
        }
        state["prompt_position"] = current.order_index + 1
    elif not complete:
        state["prompt_position"] = len(prompts) + 1

    return state


def add_rollout_prompt(rollout, text):
    order_index = len(list(rollout.prompts))
    prompt = RolloutPrompt(
        rollout_id=rollout.id,
        order_index=order_index,
        text=text,
    )
    db.session.add(prompt)
    db.session.commit()
    return prompt


def generate_rollout_candidates(rollout, prompt):
    """Create and generate six on-policy candidates for one rollout prompt."""
    if prompt.rollout_id != rollout.id:
        raise ValueError("Prompt does not belong to this rollout")

    with _prompt_lock(("rollout", prompt.id)):
        existing = list(prompt.candidates)
        used_slots = {c.slot for c in existing}
        remaining_slots = [
            slot
            for slot in range(1, rollout.outputs_per_prompt + 1)
            if slot not in used_slots
        ]
        random.shuffle(remaining_slots)
        needed = rollout.outputs_per_prompt - len(existing)
        for slot in remaining_slots[: max(0, needed)]:
            db.session.add(
                RolloutCandidate(
                    rollout_prompt_id=prompt.id,
                    slot=slot,
                    provider=rollout.provider,
                    policy_name=rollout.policy_name,
                    prompt_text=prompt.text,
                    request_payload="{}",
                    status="pending",
                )
            )
        db.session.commit()

        todo = [c for c in prompt.candidates if c.status != "ok"]
        if not todo:
            return {
                "outputs": rollout.outputs_per_prompt,
                "generated": 0,
                "failed": 0,
                **_rollout_prompt_payload(prompt),
            }

        storage_dir = _rollout_storage_dir(rollout.id)
        provider_name = rollout.provider
        prompt_text = prompt.text
        clip_seconds = rollout.clip_seconds
        semaphore = threading.Semaphore(_MAX_CONCURRENCY.get(provider_name, 2))

        def _work(candidate_id):
            with semaphore:
                result = _generate_with_retry(provider_name, prompt_text, clip_seconds)
                return candidate_id, result

        generated = 0
        failed = 0
        with ThreadPoolExecutor(max_workers=min(8, len(todo))) as ex:
            futures = {ex.submit(_work, c.id): c.id for c in todo}
            for fut in as_completed(futures):
                candidate = db.session.get(RolloutCandidate, futures[fut])
                try:
                    _, result = fut.result()
                except Exception as exc:  # noqa: BLE001 - record and keep going
                    candidate.status = "error"
                    candidate.error = repr(exc)
                    failed += 1
                    db.session.commit()
                    continue

                path = os.path.join(
                    storage_dir,
                    "candidate_%d.%s" % (candidate.id, result.audio_format),
                )
                with open(path, "wb") as f:
                    f.write(result.audio_bytes)
                candidate.audio_path = path
                candidate.audio_format = result.audio_format
                candidate.duration_ms = result.duration_ms
                candidate.request_payload = json.dumps(result.request_payload)
                candidate.status = "ok"
                candidate.error = None
                generated += 1
                db.session.commit()

    if failed:
        raise RuntimeError("%d of %d candidates failed to generate" % (failed, len(todo)))
    return {
        "outputs": rollout.outputs_per_prompt,
        "generated": generated,
        "failed": 0,
        **_rollout_prompt_payload(prompt),
    }


def rank_rollout_prompt(prompt, ranked_slots):
    if prompt.is_ranked:
        return rollout_ranking_export(prompt)

    if not isinstance(ranked_slots, list):
        raise ValueError("ranked_slots must be a list")

    try:
        slots = [int(slot) for slot in ranked_slots]
    except (TypeError, ValueError):
        raise ValueError("ranked_slots must contain slot numbers")

    rollout = prompt.rollout
    expected_slots = set(range(1, rollout.outputs_per_prompt + 1))
    if len(slots) != rollout.outputs_per_prompt or set(slots) != expected_slots:
        raise ValueError("ranked_slots must contain each slot exactly once")

    candidates = list(prompt.candidates)
    if len(candidates) != rollout.outputs_per_prompt:
        raise ValueError("all candidates must be generated before ranking")
    if any(c.status != "ok" for c in candidates):
        raise ValueError("all candidates must be generated before ranking")

    by_slot = {c.slot: c for c in candidates}
    for rank_position, slot in enumerate(slots, start=1):
        by_slot[slot].rank_position = rank_position
    prompt.ranked_at = _utcnow()
    db.session.commit()
    return rollout_ranking_export(prompt)


def rollout_summary(rollout):
    ranked = _ranked_prompt_count(rollout)
    return {
        "id": rollout.id,
        "url": "/rollout/%d" % rollout.id,
        "user_email": rollout.user_email,
        "policy_name": rollout.policy_name,
        "provider": rollout.provider,
        "ranked": ranked,
        "planned": rollout.num_prompts,
        "outputs_per_prompt": rollout.outputs_per_prompt,
        "complete": ranked == rollout.num_prompts,
    }


def rollout_summary_rows():
    rollouts = db.session.query(Rollout).order_by(Rollout.id.desc()).all()
    return [rollout_summary(rollout) for rollout in rollouts]


def rollout_overview_rows():
    rows = []
    rollouts = db.session.query(Rollout).order_by(Rollout.id.desc()).all()
    for rollout in rollouts:
        for prompt in rollout.prompts:
            ranked_candidates = sorted(
                [c for c in prompt.candidates if c.rank_position is not None],
                key=lambda c: c.rank_position,
            )
            ranking = " > ".join("#%d" % c.id for c in ranked_candidates)
            rows.append(
                {
                    "rollout_id": rollout.id,
                    "rollout_url": "/rollout/%d" % rollout.id,
                    "prompt_id": prompt.id,
                    "prompt": _truncate(prompt.text),
                    "prompt_full": " ".join(prompt.text.split()),
                    "user_email": rollout.user_email,
                    "policy_name": rollout.policy_name,
                    "status": "ranked" if prompt.is_ranked else "pending",
                    "candidate_count": len(list(prompt.candidates)),
                    "ranking": ranking or "--",
                    "best_candidate": "#%d" % ranked_candidates[0].id
                    if ranked_candidates
                    else "--",
                    "worst_candidate": "#%d" % ranked_candidates[-1].id
                    if ranked_candidates
                    else "--",
                    "ranked_at": prompt.ranked_at.isoformat()
                    if prompt.ranked_at
                    else "--",
                    "export_url": "/api/rollouts/%d/rankings" % rollout.id,
                }
            )
    return rows


def rollout_ranking_export(prompt):
    ranked_candidates = sorted(
        list(prompt.candidates),
        key=lambda c: c.rank_position if c.rank_position is not None else 999,
    )
    return {
        "rollout_id": prompt.rollout_id,
        "rollout_prompt_id": prompt.id,
        "user_email": prompt.rollout.user_email,
        "provider": prompt.rollout.provider,
        "policy_name": prompt.rollout.policy_name,
        "prompt": prompt.text,
        "ranked_at": prompt.ranked_at.isoformat() if prompt.ranked_at else None,
        "rank_semantics": "1 is best",
        "candidates": [
            {
                "candidate_id": candidate.id,
                "rank": candidate.rank_position,
                "slot": candidate.slot,
                "audio_url": "/api/rollout-candidates/%d/audio" % candidate.id,
                "audio_format": candidate.audio_format,
                "duration_ms": candidate.duration_ms,
                "request_payload": _json_loads(candidate.request_payload),
            }
            for candidate in ranked_candidates
        ],
    }


def rollout_rankings(rollout=None):
    if rollout is not None:
        prompts = list(rollout.prompts)
    else:
        prompts = (
            db.session.query(RolloutPrompt)
            .join(Rollout)
            .order_by(Rollout.id.desc(), RolloutPrompt.order_index)
            .all()
        )
    return [rollout_ranking_export(prompt) for prompt in prompts if prompt.is_ranked]
