const form = document.getElementById("convert-form");
const printerSelect = document.getElementById("printer");
const customSize = document.getElementById("custom-size");
const status = document.getElementById("status");
const submitBtn = document.getElementById("submit-btn");
const loader = document.getElementById("loader");
const loadStep = document.getElementById("load-step");
const loadBar = document.getElementById("load-bar");
const loadDetail = document.getElementById("load-detail");

printerSelect.addEventListener("change", () => {
  customSize.classList.toggle("hidden", printerSelect.value !== "custom");
});

function setStatus(message, kind) {
  status.textContent = message;
  status.className = "status" + (kind ? " " + kind : "");
}

function showLoader(show) {
  loader.classList.toggle("hidden", !show);
}

function renderProgress(p) {
  const total = Number(p.total) || 0;
  const done = Number(p.done) || 0;
  if (total > 0) {
    loadBar.removeAttribute("value");
    loadBar.max = total;
    loadBar.value = done;
  } else {
    loadBar.removeAttribute("value");
  }
  loadStep.textContent = p.step || "";
  loadDetail.textContent = total > 0 ? done + " of " + total + (p.detail ? " - " + p.detail : "") : (p.detail || "");
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function pollProgress(jobId) {
  while (true) {
    const res = await fetch("/progress/" + jobId);
    const p = await res.json();
    if (!res.ok) {
      throw new Error(p.error || "progress failed");
    }
    renderProgress(p);
    if (p.state === "done") {
      return p;
    }
    if (p.state === "error") {
      throw new Error(p.error || "conversion failed");
    }
    await sleep(500);
  }
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  submitBtn.disabled = true;
  setStatus("", "");
  showLoader(true);
  loadStep.textContent = "Starting";
  loadDetail.textContent = "";
  loadBar.removeAttribute("value");

  try {
    const startRes = await fetch("/convert", {
      method: "POST",
      body: new FormData(form),
    });
    const startData = await startRes.json().catch(() => ({}));
    if (!startRes.ok) {
      throw new Error(startData.error || "something went wrong");
    }
    await pollProgress(startData.job_id);
    setStatus("Done, downloading.", "done");
    showLoader(false);
    window.location.href = "/download/" + startData.job_id;
  } catch (err) {
    showLoader(false);
    setStatus(err.message || "something went wrong", "error");
  } finally {
    submitBtn.disabled = false;
  }
});

function wireHelpPopup(buttonId, popupId) {
  const button = document.getElementById(buttonId);
  const popup = document.getElementById(popupId);
  if (!button || !popup) {
    return null;
  }

  function close() {
    popup.hidden = true;
    button.setAttribute("aria-expanded", "false");
  }

  button.addEventListener("click", (event) => {
    event.stopPropagation();
    const willOpen = popup.hidden;
    document.querySelectorAll(".smart-help-popup").forEach((p) => {
      if (p !== popup) {
        p.hidden = true;
      }
    });
    document.querySelectorAll(".smart-help-button").forEach((b) => {
      if (b !== button) {
        b.setAttribute("aria-expanded", "false");
      }
    });
    popup.hidden = !willOpen;
    button.setAttribute("aria-expanded", String(willOpen));
  });

  popup.addEventListener("click", (event) => {
    event.stopPropagation();
  });

  document.addEventListener("click", close);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !popup.hidden) {
      close();
      button.focus();
    }
  });

  return { close };
}

wireHelpPopup("smart-rotation-help", "smart-rotation-popup");
wireHelpPopup("printer-tolerance-help", "printer-tolerance-popup");

