// Drives the rollout flow: prompt entry -> six outputs -> full ranking -> repeat.
(function () {
  const app = document.getElementById("rollout-app");
  const rolloutId = app.dataset.rolloutId;

  const el = {
    progress: document.getElementById("progress"),
    promptPhase: document.getElementById("prompt-phase"),
    promptText: document.getElementById("prompt-text"),
    promptSubmit: document.getElementById("prompt-submit"),
    generatingPhase: document.getElementById("generating-phase"),
    rankingPhase: document.getElementById("ranking-phase"),
    currentPromptText: document.getElementById("current-prompt-text"),
    candidates: document.getElementById("candidates"),
    rankingReset: document.getElementById("ranking-reset"),
    rankingList: document.getElementById("ranking-list"),
    rankingHint: document.getElementById("ranking-hint"),
    rankingSubmit: document.getElementById("ranking-submit"),
    donePhase: document.getElementById("done-phase"),
    error: document.getElementById("error"),
  };

  let currentPromptId = null;
  let candidates = [];
  let ranking = [];
  let played = {};
  let generatingInFlight = false;

  function showError(msg) {
    el.error.textContent = msg;
    el.error.hidden = false;
  }

  function clearError() {
    el.error.hidden = true;
  }

  function hidePhases() {
    el.promptPhase.hidden = true;
    el.generatingPhase.hidden = true;
    el.rankingPhase.hidden = true;
    el.donePhase.hidden = true;
  }

  async function api(path, opts) {
    const resp = await fetch(path, opts);
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.error || ("HTTP " + resp.status));
    return data;
  }

  async function loadState() {
    clearError();
    let state;
    try {
      state = await api(`/api/rollouts/${rolloutId}/state`);
    } catch (e) {
      showError(e.message);
      return;
    }
    render(state);
  }

  function render(state) {
    hidePhases();

    if (state.complete) {
      el.progress.textContent =
        `${state.num_prompts} prompts x ${state.outputs_per_prompt} outputs - complete`;
      el.donePhase.hidden = false;
      return;
    }

    const pos = state.prompt_position;
    el.progress.textContent =
      `Prompt ${pos} of ${state.num_prompts} - ${state.outputs_per_prompt} outputs - ${state.clip_seconds}s clips`;

    if (state.need_new_prompt) {
      el.promptText.value = "";
      el.promptPhase.hidden = false;
      el.promptText.focus();
      return;
    }

    const cp = state.current_prompt;
    currentPromptId = cp.id;
    el.currentPromptText.textContent = cp.text;

    if (cp.needs_generation) {
      el.progress.textContent = `Prompt ${pos} of ${state.num_prompts} - preparing outputs`;
      el.generatingPhase.hidden = false;
      generateCandidates(cp.id);
      return;
    }

    candidates = cp.candidates || [];
    ranking = [];
    played = {};
    candidates.forEach((candidate) => {
      played[candidate.slot] = false;
    });
    el.rankingPhase.hidden = false;
    renderCandidates();
    updateRanking();
  }

  async function generateCandidates(promptId) {
    if (generatingInFlight) return;
    generatingInFlight = true;
    try {
      await api(`/api/rollouts/${rolloutId}/generate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt_id: promptId }),
      });
      generatingInFlight = false;
      await loadState();
    } catch (e) {
      generatingInFlight = false;
      showError("Generation failed: " + e.message + " - refresh to retry.");
    }
  }

  function renderCandidates() {
    el.candidates.textContent = "";
    candidates.forEach((candidate) => {
      const card = document.createElement("div");
      card.className = "song rollout-candidate";
      card.dataset.slot = candidate.slot;

      const title = document.createElement("h3");
      title.textContent = `Output ${candidate.slot}`;
      card.appendChild(title);

      const audio = document.createElement("audio");
      audio.controls = true;
      audio.preload = "auto";
      audio.dataset.slot = candidate.slot;
      audio.addEventListener("loadstart", () => {
        played[candidate.slot] = false;
        updateRanking();
      });
      audio.addEventListener("play", () => {
        played[candidate.slot] = true;
        updateRanking();
      });
      audio.src = candidate.url;
      card.appendChild(audio);

      const meta = document.createElement("div");
      meta.className = "rollout-candidate-meta";

      const playedBadge = document.createElement("span");
      playedBadge.className = "played-badge";
      playedBadge.dataset.playedSlot = candidate.slot;
      playedBadge.hidden = true;
      playedBadge.textContent = "played";
      meta.appendChild(playedBadge);

      const rankBadge = document.createElement("span");
      rankBadge.className = "rank-badge";
      rankBadge.dataset.rankSlot = candidate.slot;
      rankBadge.hidden = true;
      meta.appendChild(rankBadge);

      card.appendChild(meta);

      const button = document.createElement("button");
      button.className = "btn rank-choice";
      button.type = "button";
      button.dataset.rankChoice = candidate.slot;
      button.textContent = "Add to ranking";
      button.addEventListener("click", () => addToRanking(candidate.slot));
      card.appendChild(button);

      el.candidates.appendChild(card);
    });
  }

  function addToRanking(slot) {
    const numericSlot = Number(slot);
    if (ranking.includes(numericSlot)) return;
    ranking.push(numericSlot);
    updateRanking();
  }

  function updateRanking() {
    el.rankingList.textContent = "";
    ranking.forEach((slot) => {
      const item = document.createElement("li");
      item.textContent = `Output ${slot}`;
      el.rankingList.appendChild(item);
    });

    candidates.forEach((candidate) => {
      const slot = candidate.slot;
      const rankPosition = ranking.indexOf(slot) + 1;
      const card = el.candidates.querySelector(`[data-slot="${slot}"]`);
      const button = el.candidates.querySelector(`[data-rank-choice="${slot}"]`);
      const playedBadge = el.candidates.querySelector(`[data-played-slot="${slot}"]`);
      const rankBadge = el.candidates.querySelector(`[data-rank-slot="${slot}"]`);

      if (card) card.classList.toggle("ranked", rankPosition > 0);
      if (button) {
        button.disabled = rankPosition > 0;
        button.textContent = rankPosition > 0 ? `Ranked #${rankPosition}` : "Add to ranking";
      }
      if (playedBadge) playedBadge.hidden = !played[slot];
      if (rankBadge) {
        rankBadge.hidden = rankPosition === 0;
        rankBadge.textContent = rankPosition > 0 ? `#${rankPosition}` : "";
      }
    });

    const allPlayed = candidates.length > 0 && candidates.every((c) => played[c.slot]);
    const allRanked = candidates.length > 0 && ranking.length === candidates.length;
    el.rankingSubmit.disabled = !(allPlayed && allRanked);

    if (!allPlayed) {
      el.rankingHint.textContent = "Start playing every output to unlock submission.";
      el.rankingHint.hidden = false;
    } else if (!allRanked) {
      el.rankingHint.textContent = "Add every output to the ranking.";
      el.rankingHint.hidden = false;
    } else {
      el.rankingHint.hidden = true;
    }
  }

  el.promptSubmit.addEventListener("click", async () => {
    const text = el.promptText.value.trim();
    if (!text) { showError("Enter a prompt first."); return; }
    clearError();
    el.promptSubmit.disabled = true;
    try {
      await api(`/api/rollouts/${rolloutId}/prompts`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
      await loadState();
    } catch (e) {
      showError(e.message);
    } finally {
      el.promptSubmit.disabled = false;
    }
  });

  el.rankingReset.addEventListener("click", () => {
    ranking = [];
    updateRanking();
  });

  el.rankingSubmit.addEventListener("click", async () => {
    if (!currentPromptId || ranking.length !== candidates.length) return;
    clearError();
    el.rankingSubmit.disabled = true;
    try {
      await api(`/api/rollout-prompts/${currentPromptId}/rank`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ranked_slots: ranking }),
      });
      await loadState();
    } catch (e) {
      showError(e.message);
      updateRanking();
    }
  });

  loadState();
})();
