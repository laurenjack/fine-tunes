// Drives the experiment flow:
//   prompt entry -> generate 6 candidates -> click-to-rank -> submit -> next prompt
(function () {
  const app = document.getElementById("app");
  const expId = app.dataset.expId;

  const el = {
    progress: document.getElementById("progress"),
    promptPhase: document.getElementById("prompt-phase"),
    promptText: document.getElementById("prompt-text"),
    promptSubmit: document.getElementById("prompt-submit"),
    generatingPhase: document.getElementById("generating-phase"),
    rankPhase: document.getElementById("rank-phase"),
    currentPromptText: document.getElementById("current-prompt-text"),
    cards: document.getElementById("cards"),
    submitBtn: document.getElementById("submit-ranking"),
    resetBtn: document.getElementById("reset-ranking"),
    donePhase: document.getElementById("done-phase"),
    resultsLink: document.getElementById("results-link"),
    error: document.getElementById("error"),
  };

  // Per-prompt session state.
  let currentPromptId = null;
  let currentComparisonId = null;
  let generatingInFlight = false;
  let ranking = []; // slot numbers in user-preferred order (slot at index 0 = rank 1)
  let songs = [];   // [{slot, url, format}]

  // --- helpers ---
  function showError(msg) { el.error.textContent = msg; el.error.hidden = false; }
  function clearError() { el.error.hidden = true; }
  function hidePhases() {
    el.promptPhase.hidden = true;
    el.generatingPhase.hidden = true;
    el.rankPhase.hidden = true;
    el.donePhase.hidden = true;
  }
  async function api(path, opts) {
    const resp = await fetch(path, opts);
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.error || ("HTTP " + resp.status));
    return data;
  }

  // --- state load + render ---
  async function loadState() {
    clearError();
    let state;
    try {
      state = await api(`/api/experiments/${expId}/state`);
    } catch (e) { showError(e.message); return; }
    render(state);
  }

  function render(state) {
    hidePhases();
    resetRanking();

    const kindLabel = state.kind === "rollout" ? "Rollout" : "Head-to-head";
    if (state.complete) {
      el.progress.textContent = `${kindLabel} · ${state.num_prompts} prompts ranked`;
      el.resultsLink.href = `/experiment/${expId}/results`;
      el.donePhase.hidden = false;
      return;
    }

    const pos = state.prompt_position;
    el.progress.textContent = `${kindLabel} · prompt ${pos} of ${state.num_prompts} · ${state.clip_seconds}s clips`;

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
      el.generatingPhase.hidden = false;
      kickGenerate(cp.id);
      return;
    }

    // Candidates ready: show ranking grid.
    songs = (state.open_comparison && state.open_comparison.songs) || [];
    currentComparisonId = state.open_comparison ? state.open_comparison.comparison_id : null;
    el.rankPhase.hidden = false;
    renderCards();
  }

  async function kickGenerate(promptId) {
    if (generatingInFlight) return;
    generatingInFlight = true;
    try {
      await api(`/api/experiments/${expId}/generate`, {
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

  // --- ranking UI ---
  function resetRanking() {
    ranking = [];
    el.submitBtn.disabled = true;
  }

  function renderCards() {
    el.cards.innerHTML = "";
    for (const song of songs) {
      const card = document.createElement("div");
      card.className = "card-clip";
      card.dataset.slot = String(song.slot);
      card.innerHTML = `
        <div class="card-clip-head">
          <span class="card-clip-slot">Slot ${song.slot}</span>
          <span class="card-clip-rank" data-rank></span>
        </div>
        <audio controls preload="auto" src="${song.url}"></audio>
      `;
      card.addEventListener("click", (e) => {
        // Don't trigger ranking when interacting with the audio player.
        if (e.target.closest("audio")) return;
        toggleRank(song.slot);
      });
      el.cards.appendChild(card);
    }
    paintRanks();
  }

  function toggleRank(slot) {
    const idx = ranking.indexOf(slot);
    if (idx >= 0) {
      ranking.splice(idx, 1); // un-rank: remove this slot; subsequent ranks shift down
    } else if (ranking.length < songs.length) {
      ranking.push(slot);
    }
    paintRanks();
  }

  function paintRanks() {
    for (const card of el.cards.querySelectorAll(".card-clip")) {
      const slot = Number(card.dataset.slot);
      const rank = ranking.indexOf(slot);
      const badge = card.querySelector("[data-rank]");
      if (rank >= 0) {
        card.classList.add("ranked");
        badge.textContent = `#${rank + 1}`;
      } else {
        card.classList.remove("ranked");
        badge.textContent = "";
      }
    }
    el.submitBtn.disabled = ranking.length !== songs.length;
  }

  // --- events ---
  el.promptSubmit.addEventListener("click", async () => {
    const text = el.promptText.value.trim();
    if (!text) { showError("Enter a prompt first."); return; }
    clearError();
    el.promptSubmit.disabled = true;
    try {
      await api(`/api/experiments/${expId}/prompts`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
      await loadState();
    } catch (e) { showError(e.message); }
    finally { el.promptSubmit.disabled = false; }
  });

  el.resetBtn.addEventListener("click", () => { ranking = []; paintRanks(); });

  el.submitBtn.addEventListener("click", async () => {
    if (!currentComparisonId || ranking.length !== songs.length) return;
    clearError();
    el.submitBtn.disabled = true;
    try {
      await api(`/api/comparisons/${currentComparisonId}/rank`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ranked_slots: ranking }),
      });
      await loadState();
    } catch (e) { showError(e.message); el.submitBtn.disabled = false; }
  });

  loadState();
})();
