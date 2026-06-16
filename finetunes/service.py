"""Experiment flow logic: state derivation, sampling, scoring."""
import json
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from flask import current_app

from .models import Comparison, Experiment, Generation, Prompt, db
from .providers import PROVIDER_NAMES, get_provider
from .stats import wilson_interval

# The provider we report the win rate *for*. The other provider's rate is 1 - x
# (excluding any undecided comparisons).
PRIMARY_PROVIDER = "elevenlabs"

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
    clip_seconds = current_app.config["CLIP_SECONDS"]
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
    base = current_app.config["AUDIO_STORAGE_DIR"]
    d = os.path.join(base, str(experiment_id))
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
    from datetime import datetime, timezone

    comparison.winner_slot = winner_slot
    comparison.winner_provider = gen.provider
    comparison.decided_at = datetime.now(timezone.utc)
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
        "other_provider": other_name,
        "primary_wins": primary_wins,
        "other_wins": other_wins,
        "win_rate": point,
        "ci_low": low,
        "ci_high": high,
        "confidence": 0.95,
    }
