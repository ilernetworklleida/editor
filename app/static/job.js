/* Job detail · live progress polling + auto-refresh of reels grid */

(() => {
  const jobId = window.JOB_ID;
  if (!jobId) return;

  const logEl = document.getElementById("log");
  const badge = document.getElementById("statusBadge");
  const reelsSection = document.getElementById("reelsSection");
  const reelGrid = document.getElementById("reelGrid");
  const reelCount = document.getElementById("reelCount");

  let pollInterval = 1500;
  let stopPolling = false;

  function updateBadge(status) {
    if (!badge) return;
    badge.className = `badge badge-${status}`;
    badge.textContent = status;
    // Esconde el boton de cancelar cuando ya no aplica
    const cancelBtn = document.getElementById("cancelBtn");
    if (cancelBtn) {
      cancelBtn.hidden = !["running", "queued"].includes(status);
    }
  }

  function updateProgress(p) {
    if (!p) return;
    const bar = document.getElementById("progressBar");
    const label = document.getElementById("progressLabel");
    const pct = document.getElementById("progressPercent");
    if (bar) {
      bar.style.width = p.percent + "%";
      bar.dataset.stage = p.stage;
    }
    if (label) label.textContent = p.label || p.stage;
    if (pct) pct.textContent = p.percent + "%";
  }

  // Wire cancel button
  const cancelBtn = document.getElementById("cancelBtn");
  if (cancelBtn) {
    cancelBtn.addEventListener("click", async () => {
      if (!confirm("Cancelar este job? Se matara el subprocess.")) return;
      cancelBtn.disabled = true;
      cancelBtn.textContent = "Cancelando...";
      try {
        const res = await fetch(`/job/${cancelBtn.dataset.jobId}/cancel`, { method: "POST" });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        // El polling seguira y vera status=cancelled en la siguiente vuelta
      } catch (e) {
        alert("Error cancelando: " + e.message);
        cancelBtn.disabled = false;
      }
    });
  }

  function renderReels(reels) {
    if (!reelsSection || !reelGrid) return;
    if (!reels || reels.length === 0) {
      reelsSection.hidden = true;
      return;
    }
    reelsSection.hidden = false;
    if (reelCount) reelCount.textContent = reels.length;

    // Re-render solo si cambia la cantidad
    const currentCount = reelGrid.querySelectorAll(".reel-card").length;
    if (currentCount === reels.length) return;

    reelGrid.innerHTML = "";
    reels.forEach((r) => {
      const card = document.createElement("article");
      card.className = "reel-card";
      const poster = r.thumb ? ` poster="${r.thumb}"` : "";
      const txtBlock = r.txt
        ? `
          <details class="reel-text">
            <summary>Transcripcion + hashtags</summary>
            <pre>${escapeHtml(r.txt)}</pre>
          </details>`
        : "";
      const copyBtn = r.txt
        ? `<button type="button" class="btn-link reel-copy" data-text="${escapeAttr(r.txt)}">
             <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>
             copiar texto
           </button>`
        : "";
      card.innerHTML = `
        <video class="reel-video" controls preload="metadata"${poster}>
          <source src="${r.video}" type="video/mp4">
        </video>
        <div class="reel-info">
          <div class="reel-name">${r.name} <span class="reel-size">${r.size_mb} MB</span></div>
          <div class="reel-actions">
            <a class="btn-link" href="${r.video}" download>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg>
              mp4
            </a>
            ${copyBtn}
          </div>
          ${txtBlock}
        </div>`;
      reelGrid.appendChild(card);
    });
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>]/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;"}[c]));
  }
  function escapeAttr(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  }

  async function poll() {
    if (stopPolling) return;
    try {
      const res = await fetch(`/api/job/${jobId}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      if (logEl) {
        logEl.textContent = data.log || "(esperando salida...)";
        // Auto-scroll si el usuario esta abajo
        if (Math.abs(logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight) < 60) {
          logEl.scrollTop = logEl.scrollHeight;
        }
      }
      updateBadge(data.status);
      updateProgress(data.progress);
      if (data.reels && data.reels.length) renderReels(data.reels);

      if (data.status === "done" || data.status === "error") {
        stopPolling = true;
      }
    } catch (e) {
      console.error("polling error:", e);
    }
  }

  // Browser notification cuando termina
  let notifShown = false;
  const originalUpdateBadge = updateBadge;
  updateBadge = function(status) {
    originalUpdateBadge(status);
    if (!notifShown && (status === "done" || status === "error")) {
      notifShown = true;
      tryNotify(status);
    }
  };

  function tryNotify(status) {
    if (!("Notification" in window)) return;
    const text = status === "done"
      ? "Reels listos para descargar"
      : "El job fallo - revisa el log";
    const send = () => {
      try {
        new Notification(`Editor: ${status}`, {
          body: text,
          icon: "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='%23f97316'%3E%3Cpath d='M23 7l-7 5 7 5V7zM3 5h13v14H3z'/%3E%3C/svg%3E",
        });
      } catch (e) {}
    };
    if (Notification.permission === "granted") send();
    else if (Notification.permission !== "denied") {
      Notification.requestPermission().then((p) => p === "granted" && send());
    }
  }

  poll();
  const intervalId = setInterval(() => {
    if (stopPolling) {
      clearInterval(intervalId);
      return;
    }
    poll();
  }, pollInterval);
})();
