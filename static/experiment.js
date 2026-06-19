// Drives the experiment flow:
//   prompt entry -> generate 6 candidates -> drag-to-rank -> submit -> next prompt
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
    rankList: document.getElementById("rank-list"),
    submitBtn: document.getElementById("submit-ranking"),
    donePhase: document.getElementById("done-phase"),
    resultsLink: document.getElementById("results-link"),
    error: document.getElementById("error"),
  };

  let currentPromptId = null;
  let currentComparisonId = null;
  let generatingInFlight = false;
  let dragSrc = null;

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
    try { state = await api(`/api/experiments/${expId}/state`); }
    catch (e) { showError(e.message); return; }
    render(state);
  }

  function render(state) {
    hidePhases();

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

    const open = state.open_comparison;
    currentComparisonId = open ? open.comparison_id : null;
    el.rankPhase.hidden = false;
    renderRows((open && open.songs) || []);
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

  // --- drag-to-rank ---
  function renderRows(songs) {
    el.rankList.innerHTML = "";
    for (const song of songs) {
      const row = document.createElement("div");
      row.className = "rank-row";
      row.dataset.slot = String(song.slot);
      row.innerHTML = `
        <span class="drag-handle" draggable="true" title="Drag to reorder" aria-label="Drag to reorder">≡</span>
        <span class="row-rank" data-rank></span>
        <audio controls preload="auto" src="${song.url}"></audio>
      `;
      attachDragHandlers(row);
      el.rankList.appendChild(row);
    }
    paintRanks();
    el.submitBtn.disabled = false;
  }

  function attachDragHandlers(row) {
    const handle = row.querySelector(".drag-handle");
    handle.addEventListener("dragstart", (e) => {
      dragSrc = row;
      row.classList.add("dragging");
      e.dataTransfer.effectAllowed = "move";
      // Use the whole row as the drag image, not just the tiny handle.
      try { e.dataTransfer.setDragImage(row, 20, 20); } catch (_) {}
      // Required for Firefox to actually start the drag.
      e.dataTransfer.setData("text/plain", row.dataset.slot);
    });
    handle.addEventListener("dragend", () => {
      row.classList.remove("dragging");
      clearDropHints();
      dragSrc = null;
      paintRanks();
    });
    row.addEventListener("dragover", (e) => {
      if (!dragSrc || dragSrc === row) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      const rect = row.getBoundingClientRect();
      const above = (e.clientY - rect.top) < rect.height / 2;
      clearDropHints();
      row.classList.add(above ? "drop-above" : "drop-below");
    });
    row.addEventListener("dragleave", () => {
      row.classList.remove("drop-above", "drop-below");
    });
    row.addEventListener("drop", (e) => {
      if (!dragSrc || dragSrc === row) return;
      e.preventDefault();
      const rect = row.getBoundingClientRect();
      const above = (e.clientY - rect.top) < rect.height / 2;
      el.rankList.insertBefore(dragSrc, above ? row : row.nextSibling);
      clearDropHints();
      paintRanks();
    });
  }

  function clearDropHints() {
    el.rankList.querySelectorAll(".drop-above, .drop-below").forEach((r) => {
      r.classList.remove("drop-above", "drop-below");
    });
  }

  function paintRanks() {
    el.rankList.querySelectorAll(".rank-row").forEach((row, i) => {
      row.querySelector("[data-rank]").textContent = `#${i + 1}`;
    });
  }

  function currentRanking() {
    return Array.from(el.rankList.querySelectorAll(".rank-row"))
      .map((row) => Number(row.dataset.slot));
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

  el.submitBtn.addEventListener("click", async () => {
    if (!currentComparisonId) return;
    const ranking = currentRanking();
    if (ranking.length === 0) return;
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
