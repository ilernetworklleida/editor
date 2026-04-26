/* Editor — Reels factory · UI behavior */

(() => {
  const $ = (sel, root = document) => root.querySelector(sel);

  // ===== Drag and drop + click-to-upload (home) =====
  const dropzone = $("#dropzone");
  const fileInput = $("#videoFile");
  const status = $("#uploadStatus");

  if (dropzone && fileInput) {
    dropzone.addEventListener("click", () => fileInput.click());
    dropzone.addEventListener("dragover", (e) => {
      e.preventDefault();
      dropzone.classList.add("is-drag");
    });
    dropzone.addEventListener("dragleave", () => dropzone.classList.remove("is-drag"));
    dropzone.addEventListener("drop", (e) => {
      e.preventDefault();
      dropzone.classList.remove("is-drag");
      if (e.dataTransfer.files.length) {
        uploadVideo(e.dataTransfer.files[0]);
      }
    });
    fileInput.addEventListener("change", (e) => {
      if (e.target.files.length) uploadVideo(e.target.files[0]);
    });
  }

  async function uploadVideo(file) {
    if (!status) return;
    status.hidden = false;
    status.className = "upload-status";
    status.textContent = `Subiendo ${file.name} (${(file.size / 1024 / 1024).toFixed(1)} MB)…`;

    const fd = new FormData();
    fd.append("file", file);

    try {
      const res = await fetch("/upload", { method: "POST", body: fd });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      status.classList.add("is-ok");
      status.textContent = `Subido: ${data.name} (${data.size_mb} MB). Recargando…`;
      setTimeout(() => location.reload(), 600);
    } catch (err) {
      status.classList.add("is-error");
      status.textContent = `Error subiendo: ${err.message}`;
    }
  }

  // ===== Asset uploads (music, watermark) =====
  document.querySelectorAll('input[type="file"][data-kind]').forEach((inp) => {
    inp.addEventListener("change", async (e) => {
      const file = e.target.files[0];
      if (!file) return;
      const kind = inp.dataset.kind;
      const fd = new FormData();
      fd.append("file", file);
      fd.append("kind", kind);
      const res = await fetch("/upload-asset", { method: "POST", body: fd });
      if (res.ok) {
        const data = await res.json();
        const targetSelect = $(`select[name="${kind}"]`);
        if (targetSelect && !Array.from(targetSelect.options).some((o) => o.value === data.name)) {
          const opt = document.createElement("option");
          opt.value = data.name;
          opt.textContent = data.name;
          opt.selected = true;
          targetSelect.appendChild(opt);
        }
        inp.value = "";
      } else {
        alert("Error subiendo asset");
      }
    });
  });

  // ===== Profile -> grey out individual style/grade fields =====
  const profileSelect = $("#profile");
  const fieldsLockedByProfile = ["style", "grade", "duration", "chunk"];
  if (profileSelect) {
    profileSelect.addEventListener("change", () => {
      const hasProfile = !!profileSelect.value;
      fieldsLockedByProfile.forEach((name) => {
        const el = document.querySelector(`[name="${name}"]`);
        if (el) el.disabled = hasProfile;
      });
    });
  }

  // ===== Submit handler: disable button to avoid double-submit =====
  const runForm = $("#runForm");
  if (runForm) {
    runForm.addEventListener("submit", () => {
      const btn = runForm.querySelector('button[type="submit"]');
      if (btn) {
        btn.disabled = true;
        btn.innerHTML = `
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round">
            <path d="M21 12a9 9 0 1 1-6.219-8.56"></path>
          </svg>
          Lanzando…`;
      }
    });
  }

  // ===== Copy reel txt to clipboard =====
  document.addEventListener("click", (e) => {
    const btn = e.target.closest(".reel-copy");
    if (!btn) return;
    const text = btn.dataset.text || "";
    navigator.clipboard.writeText(text).then(() => {
      const originalHTML = btn.innerHTML;
      btn.innerHTML = "✓ copiado";
      setTimeout(() => { btn.innerHTML = originalHTML; }, 1400);
    });
  });
})();
