"""Rollout ranking flow tests using the mock generator."""

from finetunes.models import RolloutCandidate, db


USER = "james.richardson.2556@gmail.com"


def _create_rollout(client, num_prompts=1, user_email=USER):
    response = client.post(
        "/api/rollouts",
        json={"user_email": user_email, "num_prompts": num_prompts},
    )
    assert response.status_code == 201
    return response.json()["id"]


def test_rollout_flow_persists_full_ranking_and_export(client):
    rollout_id = _create_rollout(client)

    state = client.get(f"/api/rollouts/{rollout_id}/state").json()
    assert state["need_new_prompt"] is True
    assert state["outputs_per_prompt"] == 6
    assert state["complete"] is False

    prompt_response = client.post(
        f"/api/rollouts/{rollout_id}/prompts",
        json={"text": "driving melodic techno with a tight low end"},
    )
    assert prompt_response.status_code == 201
    prompt_id = prompt_response.json()["prompt_id"]

    state = client.get(f"/api/rollouts/{rollout_id}/state").json()
    assert state["current_prompt"]["needs_generation"] is True

    generate_response = client.post(
        f"/api/rollouts/{rollout_id}/generate", json={"prompt_id": prompt_id}
    )
    assert generate_response.status_code == 200
    payload = generate_response.json()
    assert payload["outputs"] == 6
    assert len(payload["candidates"]) == 6
    assert {candidate["slot"] for candidate in payload["candidates"]} == {
        1,
        2,
        3,
        4,
        5,
        6,
    }

    audio_response = client.get(payload["candidates"][0]["url"])
    assert audio_response.status_code == 200
    assert len(audio_response.content) > 0

    ranked_slots = [candidate["slot"] for candidate in reversed(payload["candidates"])]
    rank_response = client.post(
        f"/api/rollout-prompts/{prompt_id}/rank",
        json={"ranked_slots": ranked_slots},
    )
    assert rank_response.status_code == 200

    state = client.get(f"/api/rollouts/{rollout_id}/state").json()
    assert state["complete"] is True
    assert state["ranked_prompts"] == 1

    rankings = client.get(f"/api/rollouts/{rollout_id}/rankings").json()["rankings"]
    assert len(rankings) == 1
    ranking = rankings[0]
    assert ranking["rank_semantics"] == "1 is best"
    assert ranking["prompt"] == "driving melodic techno with a tight low end"
    assert [candidate["rank"] for candidate in ranking["candidates"]] == [
        1,
        2,
        3,
        4,
        5,
        6,
    ]
    assert ranking["candidates"][0]["slot"] == ranked_slots[0]
    assert ranking["candidates"][0]["audio_url"].startswith(
        "/api/rollout-candidates/"
    )
    assert "params" in ranking["candidates"][0]["request_payload"]

    overview = client.get("/api/rollouts/rankings").json()["rankings"]
    assert len(overview) == 1

    page = client.get("/rollouts")
    assert page.status_code == 200
    assert "Rollout overview" in page.text
    assert "ranked" in page.text


def test_singular_rollout_route_redirects_to_rollouts(client):
    response = client.get("/rollout", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/rollouts"


def test_rollout_rejects_duplicate_or_incomplete_ranking(client):
    rollout_id = _create_rollout(client)
    prompt_id = client.post(
        f"/api/rollouts/{rollout_id}/prompts",
        json={"text": "ambient piano with soft tape noise"},
    ).json()["prompt_id"]
    client.post(f"/api/rollouts/{rollout_id}/generate", json={"prompt_id": prompt_id})

    response = client.post(
        f"/api/rollout-prompts/{prompt_id}/rank",
        json={"ranked_slots": [1, 1, 2, 3, 4, 5]},
    )
    assert response.status_code == 400

    db.session.remove()
    assert (
        db.session.query(RolloutCandidate)
        .filter_by(rollout_prompt_id=prompt_id)
        .filter(RolloutCandidate.rank_position.isnot(None))
        .count()
        == 0
    )
