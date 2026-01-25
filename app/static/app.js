const form = document.getElementById("jobForm");
const submitBtn = document.getElementById("submitBtn");
const statusBox = document.getElementById("status");
const statusText = document.getElementById("statusText");
const statusPct = document.getElementById("statusPct");
const progress = document.getElementById("progress");
const logEl = document.getElementById("log");
const resultEl = document.getElementById("result");

let currentJobId = null;
let pollTimer = null;

function setBusy(busy) {
  submitBtn.disabled = busy;
  submitBtn.setAttribute("aria-busy", busy ? "true" : "false");
}

function renderEvents(events) {
  logEl.textContent = (events || []).join("\n");
  logEl.scrollTop = logEl.scrollHeight;
}

function renderResult(data) {
  resultEl.innerHTML = "";
  if (data.status !== "finished") return;

  if (data.zip_ready) {
    const a = document.createElement("a");
    a.href = `/api/jobs/${data.job_id}/download`;
    a.textContent = "Download ZIP";
    a.setAttribute("role", "button");
    resultEl.appendChild(a);
    return;
  }

  if (data.stored) {
    const p = document.createElement("p");
    p.textContent = "Saved on server.";
    resultEl.appendChild(p);
  }
}

async function pollJob(jobId) {
  const res = await fetch(`/api/jobs/${jobId}`);
  const data = await res.json();

  statusText.textContent =
    data.status === "error" ? `Error: ${data.error || "unknown"}` : data.step;
  progress.value = data.progress || 0;
  statusPct.textContent = `${Math.round((data.progress || 0) * 100)}%`;
  renderEvents(data.events);
  renderResult(data);

  if (data.status === "finished" || data.status === "error") {
    clearInterval(pollTimer);
    pollTimer = null;
    setBusy(false);
  }
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (pollTimer) clearInterval(pollTimer);
  resultEl.innerHTML = "";
  logEl.textContent = "";
  statusBox.hidden = false;
  setBusy(true);

  const fd = new FormData(form);
  const res = await fetch("/api/jobs", { method: "POST", body: fd });
  const data = await res.json();
  if (!res.ok) {
    setBusy(false);
    statusText.textContent = `Error: ${data.error || "unknown"}`;
    return;
  }

  currentJobId = data.job_id;
  statusText.textContent = "queued";
  progress.value = 0;
  statusPct.textContent = "0%";

  pollTimer = setInterval(() => pollJob(currentJobId), 800);
  await pollJob(currentJobId);
});
