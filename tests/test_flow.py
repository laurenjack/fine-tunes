"""End-to-end flow test using the mock generator (no real API calls)."""
import time
from concurrent.futures import ThreadPoolExecutor

from finetunes import service
from finetunes.models import Comparison, Generation, db


USER = "james.richardson.2556@gmail.com"


def _create(client, num_prompts, samples, user_email=USER):
    r = client.post(
        "/api/experiments",
        json={
            "user_email": user_email,
            "num_prompts": num_prompts,
            "samples_per_prompt": samples,
        },
    )
    assert r.status_code == 201
    return r.json()["id"]


def test_ids_start_at_one_and_increment(client):
    assert _create(client, 1, 1) == 1
    assert _create(client, 1, 1) == 2


def test_root_routes_to_experiments(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/experiments"


def test_full_experiment_flow(client):
    exp_id = _create(client, num_prompts=2, samples=2)

    # Initially needs a prompt.
    state = client.get(f"/api/experiments/{exp_id}/state").json()
    assert state["need_new_prompt"] is True
    assert state["complete"] is False
    assert state["prompt_position"] == 1

    total_comparisons = 0
    for p in range(2):  # two prompts
        r = client.post(
            f"/api/experiments/{exp_id}/prompts", json={"text": f"prompt {p}"}
        )
        assert r.status_code == 201
        prompt_id = r.json()["prompt_id"]

        for _ in range(2):  # two samples per prompt
            s = client.post(
                f"/api/experiments/{exp_id}/sample", json={"prompt_id": prompt_id}
            ).json()
            assert "comparison_id" in s
            assert len(s["songs"]) == 2
            assert {song["slot"] for song in s["songs"]} == {1, 2}

            # Audio is served and anonymous (URL is by generation id only).
            audio_url = s["songs"][0]["url"]
            ar = client.get(audio_url)
            assert ar.status_code == 200
            assert len(ar.content) > 0

            cid = s["comparison_id"]
            cr = client.post(
                f"/api/comparisons/{cid}/choose", json={"winner_slot": 1}
            )
            assert cr.status_code == 200
            total_comparisons += 1

    assert total_comparisons == 4

    state = client.get(f"/api/experiments/{exp_id}/state").json()
    assert state["complete"] is True

    res = client.get(f"/api/experiments/{exp_id}/results").json()
    assert res["n"] == 4
    assert res["primary_wins"] + res["other_wins"] == 4
    assert 0.0 <= res["win_rate"] <= 1.0
    assert res["ci_low"] <= res["win_rate"] <= res["ci_high"]


def test_sample_is_idempotent_until_chosen(client):
    exp_id = _create(client, 1, 1)
    pid = client.post(
        f"/api/experiments/{exp_id}/prompts", json={"text": "x"}
    ).json()["prompt_id"]

    a = client.post(
        f"/api/experiments/{exp_id}/sample", json={"prompt_id": pid}
    ).json()
    b = client.post(
        f"/api/experiments/{exp_id}/sample", json={"prompt_id": pid}
    ).json()
    # Same undecided comparison returned, not a fresh one.
    assert a["comparison_id"] == b["comparison_id"]


def test_cannot_oversample_a_prompt(client):
    exp_id = _create(client, 1, 1)
    pid = client.post(
        f"/api/experiments/{exp_id}/prompts", json={"text": "x"}
    ).json()["prompt_id"]
    s = client.post(
        f"/api/experiments/{exp_id}/sample", json={"prompt_id": pid}
    ).json()
    client.post(f"/api/comparisons/{s['comparison_id']}/choose", json={"winner_slot": 2})

    # Prompt is full now; sampling again should be rejected.
    r = client.post(f"/api/experiments/{exp_id}/sample", json={"prompt_id": pid})
    assert r.status_code == 400


def test_invalid_experiment_params(client):
    r = client.post(
        "/api/experiments",
        json={"user_email": USER, "num_prompts": 0, "samples_per_prompt": 1},
    )
    assert r.status_code == 400


def test_unknown_or_missing_user_rejected(client):
    r = client.post(
        "/api/experiments",
        json={"num_prompts": 2, "samples_per_prompt": 2},  # no user
    )
    assert r.status_code == 400
    r = client.post(
        "/api/experiments",
        json={"user_email": "nope@example.com", "num_prompts": 2, "samples_per_prompt": 2},
    )
    assert r.status_code == 400


def test_generate_all_prepares_every_pair(client):
    exp_id = _create(client, num_prompts=1, samples=3)
    pid = client.post(
        f"/api/experiments/{exp_id}/prompts", json={"text": "disco"}
    ).json()["prompt_id"]

    # Before generating, the prompt needs generation.
    cp = client.get(f"/api/experiments/{exp_id}/state").json()["current_prompt"]
    assert cp["needs_generation"] is True

    r = client.post(f"/api/experiments/{exp_id}/generate_all", json={"prompt_id": pid})
    assert r.status_code == 200
    assert r.json()["samples"] == 3

    # Now fully generated; no more generation needed.
    cp = client.get(f"/api/experiments/{exp_id}/state").json()["current_prompt"]
    assert cp["needs_generation"] is False

    # All three pre-generated pairs can be revealed and chosen, instantly.
    seen = set()
    for _ in range(3):
        s = client.post(
            f"/api/experiments/{exp_id}/sample", json={"prompt_id": pid}
        ).json()
        seen.add(s["comparison_id"])
        client.post(
            f"/api/comparisons/{s['comparison_id']}/choose", json={"winner_slot": 1}
        )
    assert len(seen) == 3  # three distinct pre-generated pairs

    state = client.get(f"/api/experiments/{exp_id}/state").json()
    assert state["complete"] is True


def test_generate_all_is_idempotent(client):
    exp_id = _create(client, num_prompts=1, samples=2)
    pid = client.post(
        f"/api/experiments/{exp_id}/prompts", json={"text": "x"}
    ).json()["prompt_id"]
    client.post(f"/api/experiments/{exp_id}/generate_all", json={"prompt_id": pid})
    # Second call should not create extra pairs.
    r = client.post(f"/api/experiments/{exp_id}/generate_all", json={"prompt_id": pid})
    assert r.status_code == 200
    assert r.json()["generated"] == 0


def test_concurrent_generate_all_does_not_duplicate_work(client, monkeypatch):
    exp_id = _create(client, num_prompts=1, samples=2)
    pid = client.post(
        f"/api/experiments/{exp_id}/prompts", json={"text": "parallel"}
    ).json()["prompt_id"]

    original_generate = service._generate_with_retry

    def slow_generate(provider_name, prompt_text, clip_seconds, attempts=4):
        time.sleep(0.05)
        return original_generate(provider_name, prompt_text, clip_seconds, attempts)

    monkeypatch.setattr(service, "_generate_with_retry", slow_generate)

    def generate_all(_):
        return client.post(
            f"/api/experiments/{exp_id}/generate_all", json={"prompt_id": pid}
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(generate_all, range(2)))

    assert [r.status_code for r in responses] == [200, 200]
    assert sorted(r.json()["generated"] for r in responses) == [0, 4]
    assert db.session.query(Comparison).count() == 2
    assert db.session.query(Generation).count() == 4


def test_experiment_overview_lists_votes_confidence_and_winner(client):
    prompt_text = (
        "a very long cinematic synthwave prompt with driving drums and a bright "
        "lead melody that should be visibly truncated in the overview table"
    )
    exp_id = _create(client, num_prompts=1, samples=3)
    pid = client.post(
        f"/api/experiments/{exp_id}/prompts", json={"text": prompt_text}
    ).json()["prompt_id"]
    assert client.post(
        f"/api/experiments/{exp_id}/generate_all", json={"prompt_id": pid}
    ).status_code == 200

    db.session.remove()
    comparisons = (
        db.session.query(Comparison)
        .filter_by(experiment_id=exp_id)
        .order_by(Comparison.sample_index)
        .all()
    )
    other_provider = next(p for p in service.PROVIDER_NAMES if p != service.PRIMARY_PROVIDER)
    winners = [service.PRIMARY_PROVIDER, service.PRIMARY_PROVIDER, other_provider]
    for comparison, provider in zip(comparisons, winners):
        winner = next(g for g in comparison.generations if g.provider == provider)
        response = client.post(
            f"/api/comparisons/{comparison.id}/choose",
            json={"winner_slot": winner.slot},
        )
        assert response.status_code == 200

    overview = client.get("/api/experiments").json()["experiments"][0]
    assert overview["id"] == exp_id
    assert overview["prompt"].endswith("...")
    assert len(overview["prompt"]) <= 80
    assert overview["prompt_full"] == prompt_text
    assert overview["model_a_name"] == "ElevenLabs Music"
    assert overview["model_a_votes"] == 2
    assert overview["model_b_name"] == "Stable Audio 3.0 (fal)"
    assert overview["model_b_votes"] == 1
    assert overview["confidence_score"] is not None
    assert overview["confidence_score_label"].endswith("%")
    assert overview["winning_model_name"] == "ElevenLabs Music"

    page = client.get("/experiments")
    assert page.status_code == 200
    assert "Experiment overview" in page.text
    assert "ElevenLabs Music" in page.text
    assert "Stable Audio 3.0 (fal)" in page.text


def test_experiment_records_user(client):
    exp_id = _create(client, 1, 1, user_email="jacklaurenson@gmail.com")
    state = client.get(f"/api/experiments/{exp_id}/state").json()
    assert state["user_email"] == "jacklaurenson@gmail.com"
