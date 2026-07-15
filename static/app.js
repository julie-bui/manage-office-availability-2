const dropzone = document.getElementById("dropzone");
const fileInput = document.getElementById("fileInput");
const fileListEl = document.getElementById("fileList");
const processBtn = document.getElementById("processBtn");
const statusEl = document.getElementById("status");
const statusExtraEl = document.getElementById("statusExtra");
const resultsEl = document.getElementById("results");

let selectedFiles = [];

// Purely a UI-timing reassurance, not tied to file size — a small file with
// several ambiguous addresses needing extra web-search lookups (see
// extraction.address_lookup's rate-limit pacing) can take just as long as a
// large one, so this fires whenever the request itself is still running
// after this delay, regardless of why.
const SLOW_MESSAGE_DELAY_MS = 12000;
const SLOW_MESSAGE =
  "Larger or more complex files can take longer to process, especially ones with several " +
  "addresses that need extra lookups. This can take up to a couple of minutes — please don't close this page.";

// Shown for any failure that isn't a clean per-file error from the server
// (server crash/500, gateway timeout page, malformed/non-JSON response,
// etc.) — deliberately generic so we never leak a raw parse error, stack
// trace, or HTML page into the UI.
const GENERIC_ERROR_MESSAGE =
  "Something went wrong while processing your file(s). This can happen with very large files, " +
  "network issues, or a temporary server problem. Please try again — if it keeps happening, let us know.";

// Shown specifically when fetch() itself throws, which only happens when
// there was no HTTP response at all (offline, DNS failure, connection
// reset) — distinct from the server responding with an error.
const NETWORK_ERROR_MESSAGE =
  "Couldn't reach the server — check your internet connection and try again.";

function setStatus(text, isError) {
  statusEl.textContent = text;
  statusEl.classList.toggle("error", !!isError);
}

function setStatusExtra(text) {
  statusExtraEl.textContent = text || "";
}

dropzone.addEventListener("click", () => fileInput.click());
dropzone.addEventListener("dragover", (e) => {
  e.preventDefault();
  dropzone.classList.add("dragover");
});
dropzone.addEventListener("dragleave", () => dropzone.classList.remove("dragover"));
dropzone.addEventListener("drop", (e) => {
  e.preventDefault();
  dropzone.classList.remove("dragover");
  addFiles(e.dataTransfer.files);
});
fileInput.addEventListener("change", () => {
  addFiles(fileInput.files);
  fileInput.value = "";
});

function addFiles(fileListLike) {
  for (const f of fileListLike) {
    if (!selectedFiles.some((existing) => existing.name === f.name && existing.size === f.size)) {
      selectedFiles.push(f);
    }
  }
  renderFileList();
}

function removeFile(index) {
  selectedFiles.splice(index, 1);
  renderFileList();
}

function renderFileList() {
  fileListEl.innerHTML = "";
  selectedFiles.forEach((f, i) => {
    const li = document.createElement("li");
    const name = document.createElement("span");
    name.textContent = `${f.name} (${(f.size / 1024).toFixed(1)} KB)`;
    const removeBtn = document.createElement("button");
    removeBtn.textContent = "×";
    removeBtn.title = "Remove";
    removeBtn.addEventListener("click", () => removeFile(i));
    li.appendChild(name);
    li.appendChild(removeBtn);
    fileListEl.appendChild(li);
  });
  processBtn.disabled = selectedFiles.length === 0;
}

processBtn.addEventListener("click", async () => {
  if (!selectedFiles.length) return;

  const formData = new FormData();
  selectedFiles.forEach((f) => formData.append("files", f));

  processBtn.disabled = true;
  setStatus(`Processing ${selectedFiles.length} file(s)…`, false);
  setStatusExtra("");
  resultsEl.innerHTML = "";

  // Only shown if the request is still in flight after this delay — cleared
  // in `finally` below the moment processing settles, one way or the other,
  // so it never lingers alongside a "Done." or error message.
  const slowTimer = setTimeout(() => setStatusExtra(SLOW_MESSAGE), SLOW_MESSAGE_DELAY_MS);

  try {
    let res;
    try {
      res = await fetch("/api/process", {
        method: "POST",
        body: formData,
        headers: { "X-Access-Token": window.ACCESS_TOKEN || "" },
      });
    } catch (err) {
      // fetch() only rejects when no HTTP response was received at all
      // (offline, DNS failure, connection reset mid-request) — the server
      // never got a chance to reply, so this is a connection problem, not
      // a server-side one.
      setStatus(NETWORK_ERROR_MESSAGE, true);
      return;
    }

    let rawText;
    try {
      rawText = await res.text();
    } catch (err) {
      // The connection dropped while the response body was streaming in.
      setStatus(NETWORK_ERROR_MESSAGE, true);
      return;
    }

    let data;
    try {
      data = JSON.parse(rawText);
    } catch (err) {
      // Server returned something that isn't JSON at all — a crashed-worker
      // HTML page, a proxy/gateway timeout page, an empty body, etc. Never
      // surface that raw HTML or the parse error itself to the user.
      setStatus(GENERIC_ERROR_MESSAGE, true);
      return;
    }

    if (!res.ok) {
      setStatus(data && data.error ? `Error: ${data.error}` : GENERIC_ERROR_MESSAGE, true);
      return;
    }

    setStatus("Done.", false);
    renderResults(data);
    selectedFiles = [];
    renderFileList();
  } finally {
    clearTimeout(slowTimer);
    setStatusExtra("");
    processBtn.disabled = selectedFiles.length === 0;
  }
});

// Only shown when at least one file in this batch actually carries a
// warning — Gemini's free-tier address-search quota (extraction.quota/
// extraction.address_lookup) being hit partway through, or the batch
// deadline (extraction.pipeline.BATCH_DEADLINE_SECONDS) being reached
// before every ambiguous building could be looked up — so it only
// appears when it's actually relevant to this batch, not on every run.
// Deliberately calm/informational wording and styling (.quota-notice,
// not .warning-text's amber or an alarming red) since this is expected
// behavior for a free service, not something gone wrong.
const QUOTA_NOTICE =
  "Some addresses may need manual lookup due to daily limits on our free address-search " +
  "service, or occasional delays when it's busy. This is normal and not an error — see the " +
  "notes below for which file(s) this affected.";

function renderResults(data) {
  const okCount = data.files.filter((f) => f.status === "ok").length;
  const summary = document.createElement("div");
  summary.className = "summary-line";
  summary.textContent = `Generated ${okCount} spreadsheet(s) from ${data.files.length} file(s).`;
  resultsEl.appendChild(summary);

  if (data.files.some((f) => f.warning)) {
    const notice = document.createElement("div");
    notice.className = "quota-notice";
    notice.textContent = QUOTA_NOTICE;
    resultsEl.appendChild(notice);
  }

  const table = document.createElement("table");
  table.innerHTML = `
    <thead><tr><th>File</th><th>Status</th><th>Method</th><th>Records</th><th>Output</th><th>Notes</th></tr></thead>
    <tbody></tbody>`;
  const tbody = table.querySelector("tbody");

  data.files.forEach((f) => {
    const tr = document.createElement("tr");
    const badgeClass = f.status === "ok" ? "ok" : "error";
    const methodLabel = f.method
      ? f.method.startsWith("rule:")
        ? `Rule (${f.method.slice(5)})`
        : "LLM fallback"
      : "—";
    const downloadUrl =
      `/api/download/${encodeURIComponent(data.batch_id)}/${encodeURIComponent(f.output_file)}` +
      `?token=${encodeURIComponent(window.ACCESS_TOKEN || "")}`;
    const outputCell = f.output_file
      ? `<a class="download-link" href="${downloadUrl}">${escapeHtml(f.output_file)}</a>`
      : "—";
    // A file can be "ok" (its records extracted fine) and still carry a
    // warning — e.g. Gemini's daily address-search quota was hit partway
    // through, so some rows fell back further than usual. error always
    // wins when both are somehow set (it isn't in practice: pipeline only
    // ever sets one or the other for a given file). Styled distinctly from
    // a real error (.warning-text's amber vs .error-text's red) — a
    // warning here means the file itself extracted fine, paired with the
    // general reassurance note above explaining this is expected, not a
    // sign anything actually went wrong.
    const noteText = f.error || f.warning || "";
    const noteClass = f.error ? "error-text" : "warning-text";
    tr.innerHTML = `
      <td>${escapeHtml(f.filename)}</td>
      <td><span class="badge ${badgeClass}">${f.status}</span></td>
      <td class="method">${methodLabel}</td>
      <td>${f.record_count}</td>
      <td>${outputCell}</td>
      <td class="${noteClass}">${noteText ? escapeHtml(noteText) : ""}</td>`;
    tbody.appendChild(tr);
  });

  resultsEl.appendChild(table);
}

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s;
  return div.innerHTML;
}
