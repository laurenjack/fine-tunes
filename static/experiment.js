// Drives the experiment flow: prompt entry -> sampling -> blind choice -> repeat.
(function () {
  const app = document.getElementById("app");
  const expId = app.dataset.expId;

  const el = {
    progress: document.getElementById("progress"),
    promptPhase: document.getElementById("prompt-phase"),
    promptText: document.getElementById("prompt-text"),
    promptSubmit: document.getElementById("prompt-submit"),
    generatingPhase: document.getElementById("generating-phase"),
    samplePhase: document.getElementById("sample-phase"),
    currentPromptText: document.getElementById("current-prompt-text"),
    sampleBtn: document.getElementById("sample-btn"),
    compare: document.getElementById("compare"),
    chooseHint: document.getElementById("choose-hint"),
    donePhase: document.getElementById("done-phase"),
    resultsLink: document.getElementById("results-link"),
    error: document.getElementById("error"),
  };

  let currentPromptId = null;
  let currentComparisonId = null;
  let generatingInFlight = false;
  const played = { 1: false, 2: false };  // user pressed play on this clip

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
    el.samplePhase.hidden = true;
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
      state = await api(`/api/experiments/${expId}/state`);
    } catch (e) {
      showError(e.message);
      return;
    }
    render(state);
  }

  function render(state) {
    hidePhases();
    resetCompare();

    if (state.complete) {
      el.progress.textContent =
        `${state.num_prompts} prompts × ${state.samples_per_prompt} samples — complete`;
      el.resultsLink.href = `/experiment/${expId}/results`;
      el.donePhase.hidden = false;
      return;
    }

    const pos = state.prompt_position;
    el.progress.textContent =
      `Prompt ${pos} of ${state.num_prompts} · ${state.clip_seconds}s clips`;

    if (state.need_new_prompt) {
      el.promptText.value = "";
      el.promptPhase.hidden = false;
      el.promptText.focus();
      return;
    }

    const cp = state.current_prompt;
    currentPromptId = cp.id;
    el.currentPromptText.textContent = cp.text;

    // Pairs are generated up front the moment the prompt is entered.
    if (cp.needs_generation) {
      el.progress.textContent = `Prompt ${pos} of ${state.num_prompts} · preparing samples`;
      el.generatingPhase.hidden = false;
      generateAll(cp.id);
      return;
    }

    // Ready: the listener reveals each pre-generated pair via "Sample Next".
    el.progress.textContent +=
      ` · sample ${cp.samples_done + 1} of ${state.samples_per_prompt}`;
    el.samplePhase.hidden = false;
  }

  async function generateAll(promptId) {
    if (generatingInFlight) return;
    generatingInFlight = true;
    try {
      await api(`/api/experiments/${expId}/generate_all`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt_id: promptId }),
      });
      generatingInFlight = false;
      await loadState();
    } catch (e) {
      generatingInFlight = false;
      showError("Generation failed: " + e.message + " — refresh to retry.");
    }
  }

  function resetCompare() {
    el.compare.hidden = true;
    el.sampleBtn.hidden = false;
    el.sampleBtn.disabled = false;
    el.sampleBtn.textContent = "Sample Next";
    currentComparisonId = null;
    played[1] = played[2] = false;
    document.querySelectorAll(".choice").forEach((b) => (b.disabled = true));
    document.querySelectorAll(".played-badge").forEach((b) => (b.hidden = true));
    document.querySelectorAll("audio[data-slot]").forEach((a) => {
      a.pause();
      a.removeAttribute("src");
    });
    el.chooseHint.hidden = false;
  }

  function showComparison(payload) {
    currentComparisonId = payload.comparison_id;
    el.sampleBtn.hidden = true;
    el.compare.hidden = false;
    // Fresh pair: nothing has been listened to yet.
    played[1] = played[2] = false;
    document.querySelectorAll(".choice").forEach((b) => (b.disabled = true));
    document.querySelectorAll(".played-badge").forEach((b) => (b.hidden = true));
    el.chooseHint.hidden = false;
    payload.songs.forEach((song) => {
      const audio = document.querySelector(`audio[data-slot="${song.slot}"]`);
      audio.src = song.url;
    });
  }

  function maybeEnableChoices() {
    if (played[1] && played[2]) {
      document.querySelectorAll(".choice").forEach((b) => (b.disabled = false));
      el.chooseHint.hidden = true;
    }
  }

  // --- Wire up events ---
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
    } catch (e) {
      showError(e.message);
    } finally {
      el.promptSubmit.disabled = false;
    }
  });

  el.sampleBtn.addEventListener("click", async () => {
    clearError();
    el.sampleBtn.disabled = true;
    el.sampleBtn.textContent = "Loading…";
    try {
      const payload = await api(`/api/experiments/${expId}/sample`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt_id: currentPromptId }),
      });
      showComparison(payload);
    } catch (e) {
      showError(e.message);
      el.sampleBtn.disabled = false;
      el.sampleBtn.textContent = "Sample Next";
    }
  });

  document.querySelectorAll("audio[data-slot]").forEach((audio) => {
    const slot = audio.dataset.slot;
    // A new clip began loading — clear any stale "played" state for this slot.
    audio.addEventListener("loadstart", () => {
      played[slot] = false;
      document.querySelector(`.played-badge[data-slot="${slot}"]`).hidden = true;
      document.querySelectorAll(".choice").forEach((b) => (b.disabled = true));
      el.chooseHint.hidden = false;
    });
    // Pressing play on a clip counts as having heard it — no need to finish it.
    audio.addEventListener("play", () => {
      played[slot] = true;
      document.querySelector(`.played-badge[data-slot="${slot}"]`).hidden = false;
      maybeEnableChoices();
    });
  });

  document.querySelectorAll(".choice").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!currentComparisonId) return;
      const winner = Number(btn.dataset.choice);
      document.querySelectorAll(".choice").forEach((b) => (b.disabled = true));
      try {
        await api(`/api/comparisons/${currentComparisonId}/choose`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ winner_slot: winner }),
        });
        await loadState();
      } catch (e) {
        showError(e.message);
        document.querySelectorAll(".choice").forEach((b) => (b.disabled = false));
      }
    });
  });

  loadState();
})();
