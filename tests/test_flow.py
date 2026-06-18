"""End-to-end flow tests using the mock generator (no real API calls)."""
import time
from concurrent.futures import ThreadPoolExecutor

from finetunes import service
from finetunes.models import (
    CANDIDATES_PER_PROMPT,
    HALF_CANDIDATES,
    Comparison,
    Experiment,
    Generation,
    db,
)


USER = "james.richardson.2556@gmail.com"


def _create(client, kind="head_to_head", num_prompts=1, model_a="elevenlabs", model_b="stable_audio", user_email=USER):
    body = {
        "user_email": user_email,
        "kind": kind,
        "model_a": model_a,
        "num_prompts": num_prompts,
    }
    if kind == "head_to_head":
        body["model_b"] = model_b
    r = client.post("/api/experiments", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _add_prompt(client, exp_id, text):
    r = client.post(f"/api/experiments/{exp_id}/prompts", json={"text": text})
    assert r.status_code == 201, r.text
    return r.json()["prompt_id"]


def _generate(client, exp_id, prompt_id):
    r = client.post(f"/api/experiments/{exp_id}/generate", json={"prompt_id": prompt_id})
    assert r.status_code == 200, r.text
    return r.json()


def _rank(client, comparison_id, ranked_slots):
    r = client.post(f"/api/comparisons/{comparison_id}/rank", json={"ranked_slots": ranked_slots})
    assert r.status_code == 200, r.text


# --------------------------------------------------------------------------- #
# Basic plumbing
# --------------------------------------------------------------------------- #
def test_ids_start_at_one_and_increment(client):
    assert _create(client) == 1
    assert _create(client) == 2


def test_root_redirects_to_experiments(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/experiments"


def test_invalid_experiment_params(client):
    r = client.post(
        "/api/experiments",
        json={"user_email": USER, "kind": "head_to_head", "model_a": "elevenlabs", "model_b": "stable_audio", "num_prompts": 0},
    )
    assert r.status_code == 400


def test_unknown_or_missing_user_rejected(client):
    r = client.post("/api/experiments", json={"kind": "head_to_head", "model_a": "elevenlabs", "model_b": "stable_audio", "num_prompts": 1})
    assert r.status_code == 400
    r = client.post(
        "/api/experiments",
        json={"user_email": "nope@example.com", "kind": "head_to_head", "model_a": "elevenlabs", "model_b": "stable_audio", "num_prompts": 1},
    )
    assert r.status_code == 400


def test_unknown_kind_rejected(client):
    r = client.post(
        "/api/experiments",
        json={"user_email": USER, "kind": "bogus", "model_a": "elevenlabs", "model_b": "stable_audio", "num_prompts": 1},
    )
    assert r.status_code == 400


def test_unknown_model_rejected(client):
    r = client.post(
        "/api/experiments",
        json={"user_email": USER, "kind": "head_to_head", "model_a": "elevenlabs", "model_b": "not-a-model", "num_prompts": 1},
    )
    assert r.status_code == 400


def test_h2h_requires_model_b(client):
    r = client.post(
        "/api/experiments",
        json={"user_email": USER, "kind": "head_to_head", "model_a": "elevenlabs", "num_prompts": 1},
    )
    assert r.status_code == 400


def test_experiment_records_user_kind_and_models(client):
    exp_id = _create(client, kind="head_to_head", model_a="elevenlabs", model_b="stable_audio", user_email="jacklaurenson@gmail.com")
    state = client.get(f"/api/experiments/{exp_id}/state").json()
    assert state["user_email"] == "jacklaurenson@gmail.com"
    assert state["kind"] == "head_to_head"
    assert state["model_a"] == "elevenlabs"
    assert state["model_b"] == "stable_audio"


# --------------------------------------------------------------------------- #
# Head-to-head flow (different models)
# --------------------------------------------------------------------------- #
def test_h2h_full_flow_and_scoring(client):
    exp_id = _create(client, kind="head_to_head", num_prompts=2)

    for i in range(2):
        pid = _add_prompt(client, exp_id, f"prompt {i}")
        result = _generate(client, exp_id, pid)
        assert result["candidates"] == CANDIDATES_PER_PROMPT

        # State now exposes a ranking comparison with 6 candidates in random slots.
        state = client.get(f"/api/experiments/{exp_id}/state").json()
        oc = state["open_comparison"]
        slots = [s["slot"] for s in oc["songs"]]
        assert sorted(slots) == list(range(1, CANDIDATES_PER_PROMPT + 1))

        # Audio is served.
        ar = client.get(oc["songs"][0]["url"])
        assert ar.status_code == 200 and ar.content

        # Submit a complete ranking.
        _rank(client, oc["comparison_id"], slots)

    state = client.get(f"/api/experiments/{exp_id}/state").json()
    assert state["complete"] is True

    res = client.get(f"/api/experiments/{exp_id}/results").json()
    # 2 prompts * 3*3 cross-pairs each = 18 total.
    assert res["n_pairs"] == 2 * HALF_CANDIDATES * HALF_CANDIDATES
    assert res["n_prompts_ranked"] == 2
    assert 0.0 <= res["win_rate"] <= 1.0
    assert res["ci_low"] <= res["win_rate"] <= res["ci_high"]


def test_h2h_a_winning_ranking_produces_higher_a_win_rate(client):
    """Ranking model_a's candidates first should push win rate above 50%."""
    exp_id = _create(client, kind="head_to_head", num_prompts=1)
    pid = _add_prompt(client, exp_id, "anything")
    _generate(client, exp_id, pid)

    db.session.remove()
    comp = db.session.query(Comparison).filter_by(experiment_id=exp_id).one()
    # Sort: side 'a' first, then side 'b'. Ranking those slots top-to-bottom
    # means every cross-pair is an A win → win_rate == 1.0.
    a_slots = sorted(g.slot for g in comp.generations if g.side == "a")
    b_slots = sorted(g.slot for g in comp.generations if g.side == "b")
    _rank(client, comp.id, a_slots + b_slots)

    res = client.get(f"/api/experiments/{exp_id}/results").json()
    assert res["win_rate"] == 1.0
    assert res["a_wins"] == HALF_CANDIDATES * HALF_CANDIDATES
    assert res["b_wins"] == 0


def test_h2h_different_models_shuffle_slots_between_sides(client):
    """A != B: each side's candidates should land in a mix of slot positions."""
    exp_id = _create(client, kind="head_to_head", num_prompts=4)

    db.session.remove()
    seen_a_in_high_slot = False
    seen_b_in_low_slot = False
    for i in range(4):
        pid = _add_prompt(client, exp_id, f"p{i}")
        _generate(client, exp_id, pid)
        db.session.remove()
        comps = db.session.query(Comparison).filter_by(experiment_id=exp_id).all()
        comp = comps[i]
        for g in comp.generations:
            if g.side == "a" and g.slot > HALF_CANDIDATES:
                seen_a_in_high_slot = True
            if g.side == "b" and g.slot <= HALF_CANDIDATES:
                seen_b_in_low_slot = True
        # Rank with provided slots so the next prompt is the active one.
        slots = list(range(1, CANDIDATES_PER_PROMPT + 1))
        _rank(client, comp.id, slots)
    assert seen_a_in_high_slot
    assert seen_b_in_low_slot


# --------------------------------------------------------------------------- #
# Same-model H2H = position bias check
# --------------------------------------------------------------------------- #
def test_same_model_h2h_uses_deterministic_slot_layout(client):
    exp_id = _create(client, kind="head_to_head", model_a="elevenlabs", model_b="elevenlabs", num_prompts=1)
    pid = _add_prompt(client, exp_id, "anything")
    _generate(client, exp_id, pid)

    db.session.remove()
    comp = db.session.query(Comparison).filter_by(experiment_id=exp_id).one()
    side_by_slot = {g.slot: g.side for g in comp.generations}
    # Side 'a' lives in low slots, side 'b' in high slots.
    for slot in range(1, HALF_CANDIDATES + 1):
        assert side_by_slot[slot] == "a", side_by_slot
    for slot in range(HALF_CANDIDATES + 1, CANDIDATES_PER_PROMPT + 1):
        assert side_by_slot[slot] == "b", side_by_slot


def test_same_model_h2h_results_label_uses_slot_ranges(client):
    exp_id = _create(client, kind="head_to_head", model_a="elevenlabs", model_b="elevenlabs", num_prompts=1)
    pid = _add_prompt(client, exp_id, "anything")
    _generate(client, exp_id, pid)

    db.session.remove()
    comp = db.session.query(Comparison).filter_by(experiment_id=exp_id).one()
    _rank(client, comp.id, list(range(1, CANDIDATES_PER_PROMPT + 1)))

    page = client.get(f"/experiment/{exp_id}/results")
    assert page.status_code == 200
    assert "Slots 1-3" in page.text
    assert "Slots 4-6" in page.text


# --------------------------------------------------------------------------- #
# Rollout flow
# --------------------------------------------------------------------------- #
def test_rollout_full_flow_and_preference_export(client):
    exp_id = _create(client, kind="rollout", model_a="elevenlabs", num_prompts=2)

    state = client.get(f"/api/experiments/{exp_id}/state").json()
    assert state["kind"] == "rollout"
    assert state["model_a"] == "elevenlabs"
    assert state["model_b"] is None

    for i in range(2):
        pid = _add_prompt(client, exp_id, f"prompt {i}")
        _generate(client, exp_id, pid)
        oc = client.get(f"/api/experiments/{exp_id}/state").json()["open_comparison"]
        _rank(client, oc["comparison_id"], [s["slot"] for s in oc["songs"]])

    res = client.get(f"/api/experiments/{exp_id}/results").json()
    assert res["kind"] == "rollout"
    assert res["n_prompts_ranked"] == 2
    # 6 candidates per prompt → C(6,2) = 15 preference pairs each, 30 total.
    assert res["pairs_per_prompt"] == 15
    assert res["preference_pairs"] == 30

    # Preference-pair export
    prefs = client.get(f"/api/experiments/{exp_id}/preferences").json()
    assert prefs["kind"] == "rollout"
    assert len(prefs["prompts"]) == 2
    pairs = prefs["prompts"][0]["preference_pairs"]
    assert len(pairs) == 15
    assert all("preferred" in p and "rejected" in p for p in pairs)
    # rank 1 (best) is preferred over every other rank in that prompt.
    rank_1_id = prefs["prompts"][0]["candidates"][0]["generation_id"]
    rank_1_pairs = [p for p in pairs if p["preferred"]["generation_id"] == rank_1_id]
    assert len(rank_1_pairs) == 5


def test_rollout_all_candidates_from_model_a(client):
    exp_id = _create(client, kind="rollout", model_a="elevenlabs", num_prompts=1)
    pid = _add_prompt(client, exp_id, "x")
    _generate(client, exp_id, pid)
    db.session.remove()
    comp = db.session.query(Comparison).filter_by(experiment_id=exp_id).one()
    providers = [g.provider for g in comp.generations]
    sides = [g.side for g in comp.generations]
    assert providers == ["elevenlabs"] * CANDIDATES_PER_PROMPT
    assert sides == ["a"] * CANDIDATES_PER_PROMPT


def test_rollout_export_only_for_rollouts(client):
    exp_id = _create(client, kind="head_to_head", num_prompts=1)
    r = client.get(f"/api/experiments/{exp_id}/preferences")
    assert r.status_code == 400


# --------------------------------------------------------------------------- #
# Generation behaviour
# --------------------------------------------------------------------------- #
def test_generate_is_idempotent(client):
    exp_id = _create(client, kind="head_to_head", num_prompts=1)
    pid = _add_prompt(client, exp_id, "x")
    r1 = client.post(f"/api/experiments/{exp_id}/generate", json={"prompt_id": pid}).json()
    r2 = client.post(f"/api/experiments/{exp_id}/generate", json={"prompt_id": pid}).json()
    assert r1["candidates"] == CANDIDATES_PER_PROMPT
    assert r2["generated"] == 0


def test_concurrent_generate_does_not_duplicate(client, monkeypatch):
    exp_id = _create(client, kind="head_to_head", num_prompts=1)
    pid = _add_prompt(client, exp_id, "parallel")

    original_generate = service._generate_with_retry

    def slow_generate(provider_name, prompt_text, clip_seconds, attempts=4):
        time.sleep(0.05)
        return original_generate(provider_name, prompt_text, clip_seconds, attempts)

    monkeypatch.setattr(service, "_generate_with_retry", slow_generate)

    def generate(_):
        return client.post(f"/api/experiments/{exp_id}/generate", json={"prompt_id": pid})

    with ThreadPoolExecutor(max_workers=2) as ex:
        responses = list(ex.map(generate, range(2)))

    assert [r.status_code for r in responses] == [200, 200]
    counts = sorted(r.json()["generated"] for r in responses)
    assert counts == [0, CANDIDATES_PER_PROMPT]
    assert db.session.query(Comparison).count() == 1
    assert db.session.query(Generation).count() == CANDIDATES_PER_PROMPT


# --------------------------------------------------------------------------- #
# Ranking validation
# --------------------------------------------------------------------------- #
def test_ranking_must_be_permutation(client):
    exp_id = _create(client, kind="head_to_head", num_prompts=1)
    pid = _add_prompt(client, exp_id, "x")
    _generate(client, exp_id, pid)
    comp_id = client.get(f"/api/experiments/{exp_id}/state").json()["open_comparison"]["comparison_id"]
    # Wrong length
    r = client.post(f"/api/comparisons/{comp_id}/rank", json={"ranked_slots": [1, 2, 3]})
    assert r.status_code == 400
    # Duplicate slot
    r = client.post(f"/api/comparisons/{comp_id}/rank", json={"ranked_slots": [1, 1, 2, 3, 4, 5]})
    assert r.status_code == 400
    # Right shape works
    r = client.post(f"/api/comparisons/{comp_id}/rank", json={"ranked_slots": [1, 2, 3, 4, 5, 6]})
    assert r.status_code == 200


def test_ranking_is_idempotent(client):
    exp_id = _create(client, kind="head_to_head", num_prompts=1)
    pid = _add_prompt(client, exp_id, "x")
    _generate(client, exp_id, pid)
    comp_id = client.get(f"/api/experiments/{exp_id}/state").json()["open_comparison"]["comparison_id"]
    _rank(client, comp_id, [1, 2, 3, 4, 5, 6])
    # Resubmit a different order: should be ignored.
    r = client.post(f"/api/comparisons/{comp_id}/rank", json={"ranked_slots": [6, 5, 4, 3, 2, 1]})
    assert r.status_code == 200
    db.session.remove()
    comp = db.session.get(Comparison, comp_id)
    # Ranks were not overwritten — still match the initial submission.
    ranks_by_slot = {g.slot: g.rank_position for g in comp.generations}
    assert ranks_by_slot == {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6}


# --------------------------------------------------------------------------- #
# Overview page
# --------------------------------------------------------------------------- #
def test_overview_separates_h2h_and_rollout(client):
    h2h_id = _create(client, kind="head_to_head", num_prompts=1)
    rollout_id = _create(client, kind="rollout", model_a="elevenlabs", num_prompts=1)

    rows = client.get("/api/experiments").json()
    assert {r["id"] for r in rows["head_to_head"]} == {h2h_id}
    assert {r["id"] for r in rows["rollout"]} == {rollout_id}

    page = client.get("/experiments")
    assert page.status_code == 200
    assert "Head-to-head overview" in page.text
    assert "Rollout overview" in page.text
