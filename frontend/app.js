// ===========================================================================
// State + API helper
// ===========================================================================

let state = {
  token: localStorage.getItem("iae_token") || null,
  user: JSON.parse(localStorage.getItem("iae_user") || "null"),
  profile: null,
  skills: [],
  jobs: [],
  jobFilters: { track: "ALL", search: "" },
  notifications: [],
  allocation: { internships: [], allocations: [], fairness: null },
  pipeline: { cycleId: null, interviews: [] },
  companyFilter: "",
};

async function api(path, options = {}) {
  const headers = options.headers || {};
  if (state.token) headers["Authorization"] = `Bearer ${state.token}`;
  if (options.json) {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(options.json);
  }
  const res = await fetch(path, { ...options, headers });
  if (res.status === 401) {
    logout();
    throw new Error("Session expired — please sign in again");
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `Request failed (${res.status})`);
  }
  if (res.status === 204) return null;
  return res.json();
}

// Wrap a button click handler so the button visibly disables + spins while
// the async work runs, and always restores itself afterward (even on error).
// This is the fix for the "did it freeze?" moment on long-running calls
// like Prepare cycle / Finalize / Greenhouse ingestion.
function withLoading(button, handler) {
  button.addEventListener("click", async () => {
    if (button.disabled) return;
    button.classList.add("loading");
    button.setAttribute("aria-busy", "true");
    button.disabled = true;
    try {
      await handler();
    } finally {
      button.classList.remove("loading");
      button.removeAttribute("aria-busy");
      button.disabled = false;
    }
  });
}

// ===========================================================================
// Pipeline stepper (mirrors the 4-stage workflow diagram from the deck)
// ===========================================================================

const PIPELINE_STAGES = ["collect", "score", "interview", "finalize"];

function setPipelineStage(activeStage) {
  const idx = PIPELINE_STAGES.indexOf(activeStage);
  document.querySelectorAll("#pipeline-track .pipeline-node").forEach((node) => {
    const nodeIdx = PIPELINE_STAGES.indexOf(node.dataset.stage);
    node.classList.remove("active", "done");
    if (nodeIdx < idx) node.classList.add("done");
    else if (nodeIdx === idx) node.classList.add("active");
  });
}

// ===========================================================================
// Auth
// ===========================================================================

const authScreen = document.getElementById("auth-screen");
const appScreen = document.getElementById("app-screen");

document.getElementById("show-register").addEventListener("click", () => {
  document.getElementById("login-form").style.display = "none";
  document.getElementById("register-form").style.display = "block";
});
document.getElementById("show-login").addEventListener("click", () => {
  document.getElementById("register-form").style.display = "none";
  document.getElementById("login-form").style.display = "block";
});

function showAuthError(msg) {
  document.getElementById("auth-error-slot").innerHTML = `<div class="form-error">${msg}</div>`;
}

withLoading(document.getElementById("login-submit"), async () => {
  const email = document.getElementById("login-email").value.trim();
  const password = document.getElementById("login-password").value;
  if (!email || !password) { showAuthError("Enter both an email and a password."); return; }
  try {
    const result = await api("/api/auth/login", { method: "POST", json: { email, password } });
    onAuthSuccess(result);
  } catch (err) {
    showAuthError(err.message);
  }
});

document.getElementById("register-role").addEventListener("change", (e) => {
  document.getElementById("register-company-name-field").style.display = e.target.value === "company" ? "block" : "none";
});

withLoading(document.getElementById("register-submit"), async () => {
  const name = document.getElementById("register-name").value.trim();
  const email = document.getElementById("register-email").value.trim();
  const password = document.getElementById("register-password").value;
  const role = document.getElementById("register-role").value;
  const companyName = document.getElementById("register-company-name").value.trim();
  if (!name || !email || !password) { showAuthError("Fill in name, email, and password."); return; }
  if (password.length < 8) { showAuthError("Password must be at least 8 characters."); return; }
  if (role === "company" && !companyName) { showAuthError("Enter your company name."); return; }
  try {
    const result = await api("/api/auth/register", {
      method: "POST",
      json: { name, email, password, role, company_name: role === "company" ? companyName : null },
    });
    onAuthSuccess(result);
  } catch (err) {
    showAuthError(err.message);
  }
});

document.getElementById("logout-btn").addEventListener("click", async () => {
  try { await api("/api/auth/logout", { method: "POST" }); } catch (e) { /* ignore */ }
  logout();
});

function onAuthSuccess(result) {
  state.token = result.token;
  state.user = { name: result.name, email: result.email, is_admin: result.is_admin, role: result.role, company_name: result.company_name };
  localStorage.setItem("iae_token", state.token);
  localStorage.setItem("iae_user", JSON.stringify(state.user));
  enterApp();
}

function logout() {
  state.token = null;
  state.user = null;
  localStorage.removeItem("iae_token");
  localStorage.removeItem("iae_user");
  appScreen.style.display = "none";
  authScreen.style.display = "block";
}

async function enterApp() {
  authScreen.style.display = "none";
  appScreen.style.display = "block";
  const isCompany = state.user.role === "company";
  const roleLabel = state.user.is_admin ? "admin" : isCompany ? `company · ${state.user.company_name}` : "member";
  document.getElementById("user-name-display").textContent = `${state.user.name} (${roleLabel})`;
  document.getElementById("allocation-tab-btn").style.display = state.user.is_admin ? "inline-block" : "none";
  document.getElementById("admin-ingest-panel").style.display = state.user.is_admin ? "block" : "none";
  document.getElementById("company-tab-btn").style.display = isCompany ? "inline-block" : "none";
  document.getElementById("profile-tab-btn").style.display = isCompany ? "none" : "inline-block";
  document.getElementById("notifications-tab-btn").style.display = isCompany ? "none" : "inline-block";

  if (isCompany) {
    document.getElementById("company-name-banner").textContent = `Signed in as ${state.user.company_name}. Postings and interviews below are always scoped to your company only.`;
    setActiveTab("company");
    await loadJobs();
    await loadCompanyConsole();
  } else {
    setActiveTab("profile");
    await loadProfile();
    await loadJobs();
    await loadNotifications();
    if (state.user.is_admin) setPipelineStage("collect");
  }
}

// try to resume a session on page load
(async function initialLoad() {
  try {
    const health = await fetch("/api/health").then((r) => r.json());
    state.aiEnabled = !!health.ai_summary_enabled;
    const note = document.getElementById("parser-mode-note");
    const isClaude = health.resume_parser === "claude";
    note.innerHTML = isClaude
      ? `<span class="ai-badge"><span class="dot"></span>Powered by Claude AI — resume parsing &amp; match explanations</span>`
      : `<span class="ai-badge heuristic"><span class="dot"></span>Heuristic parser active (set ANTHROPIC_API_KEY to enable Claude-powered AI)</span>`;
  } catch (e) { /* non-critical */ }

  if (!state.token) return;
  try {
    const me = await api("/api/auth/me");
    state.user = { name: me.name, email: me.email, is_admin: me.is_admin, role: me.role, company_name: me.company_name };
    localStorage.setItem("iae_user", JSON.stringify(state.user));
    await enterApp();
  } catch (err) {
    logout();
  }
})();

// ===========================================================================
// Tabs
// ===========================================================================

document.querySelectorAll(".nav-tabs button[data-tab]").forEach((btn) => {
  btn.addEventListener("click", () => setActiveTab(btn.dataset.tab));
});

function setActiveTab(name) {
  document.querySelectorAll(".nav-tabs button[data-tab]").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab-panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${name}`));
}

// ===========================================================================
// Profile / resume
// ===========================================================================

withLoading(document.getElementById("upload-resume-btn"), async () => {
  const fileInput = document.getElementById("resume-file");
  const statusEl = document.getElementById("upload-status");
  if (!fileInput.files.length) {
    statusEl.textContent = "Choose a file first.";
    return;
  }
  const file = fileInput.files[0];
  const okExt = /\.(pdf|docx|txt|md)$/i.test(file.name);
  if (!okExt) {
    statusEl.textContent = "Unsupported file type — please upload a PDF, DOCX, or TXT file.";
    return;
  }
  if (file.size > 8 * 1024 * 1024) {
    statusEl.textContent = "File is too large — max 8MB.";
    return;
  }
  const formData = new FormData();
  formData.append("file", file);
  statusEl.textContent = "Uploading and parsing…";
  try {
    const headers = {};
    if (state.token) headers["Authorization"] = `Bearer ${state.token}`;
    const res = await fetch("/api/resume/upload", { method: "POST", headers, body: formData });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || "Upload failed");
    }
    const result = await res.json();
    statusEl.textContent = `Parsed successfully. ${result.matches_found} existing posting(s) already match your profile — check Notifications.`;
    applyProfile(result.profile);
    await loadNotifications();
  } catch (err) {
    statusEl.textContent = "Error: " + err.message;
  }
});

async function loadProfile() {
  try {
    const profile = await api("/api/profile/me");
    applyProfile(profile);
  } catch (err) {
    document.getElementById("profile-panel").style.display = "none";
  }
}

function applyProfile(profile) {
  state.profile = profile;
  state.skills = [...profile.skills];
  document.getElementById("profile-panel").style.display = "block";
  document.getElementById("profile-name").value = profile.name;
  document.getElementById("profile-email").value = profile.email;
  document.getElementById("profile-category").value = profile.category;
  document.getElementById("profile-cgpa").value = profile.cgpa ?? "";
  document.getElementById("profile-state").value = profile.state || "";
  document.getElementById("profile-locations").value = (profile.preferred_locations || []).join(", ");
  renderSkillTags();
}

function renderSkillTags() {
  const container = document.getElementById("profile-skill-tags");
  container.innerHTML = state.skills.map((s, idx) => `
    <span class="skill-tag">${s}<button data-idx="${idx}" class="remove-skill">×</button></span>
  `).join("");
  container.querySelectorAll(".remove-skill").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.skills.splice(Number(btn.dataset.idx), 1);
      renderSkillTags();
    });
  });
}

document.getElementById("add-skill-btn").addEventListener("click", () => {
  const input = document.getElementById("new-skill-input");
  const value = input.value.trim();
  if (value && !state.skills.includes(value)) {
    state.skills.push(value);
    renderSkillTags();
  }
  input.value = "";
});
document.getElementById("new-skill-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); document.getElementById("add-skill-btn").click(); }
});

withLoading(document.getElementById("save-profile-btn"), async () => {
  const statusEl = document.getElementById("save-status");
  const locations = document.getElementById("profile-locations").value
    .split(",").map((s) => s.trim()).filter(Boolean);
  const cgpaRaw = document.getElementById("profile-cgpa").value;

  if (cgpaRaw !== "" && (Number.isNaN(Number(cgpaRaw)) || Number(cgpaRaw) < 0 || Number(cgpaRaw) > 10)) {
    statusEl.textContent = "CGPA must be a number between 0 and 10.";
    return;
  }

  try {
    const updated = await api("/api/profile/me", {
      method: "PUT",
      json: {
        category: document.getElementById("profile-category").value,
        state: document.getElementById("profile-state").value.trim() || null,
        preferred_locations: locations,
        skills: state.skills,
        cgpa: cgpaRaw === "" ? null : Number(cgpaRaw),
        profile_confirmed: true,
      },
    });
    applyProfile(updated);
    statusEl.textContent = "Saved.";
    setTimeout(() => (statusEl.textContent = ""), 2500);
  } catch (err) {
    statusEl.textContent = "Error: " + err.message;
  }
});

// ===========================================================================
// Job board
// ===========================================================================

async function loadJobs() {
  state.jobs = await api("/api/internships");
  renderJobGrid();
}

document.getElementById("refresh-jobs-btn").addEventListener("click", loadJobs);
document.getElementById("job-filter-track").addEventListener("change", (e) => {
  state.jobFilters.track = e.target.value;
  renderJobGrid();
});
document.getElementById("job-filter-search").addEventListener("input", (e) => {
  state.jobFilters.search = e.target.value.trim().toLowerCase();
  renderJobGrid();
});

function renderJobGrid() {
  const { track, search } = state.jobFilters;
  const filtered = state.jobs.filter((j) => {
    if (track === "SCHEME" && j.is_open_market) return false;
    if (track === "OPEN" && !j.is_open_market) return false;
    if (search && !(`${j.title} ${j.company}`.toLowerCase().includes(search))) return false;
    return true;
  });

  const grid = document.getElementById("job-grid");
  if (!filtered.length) {
    grid.innerHTML = `<div class="empty-state"><h3>No postings match</h3><p>Try a different filter, or add one via the admin panel above.</p></div>`;
    return;
  }

  grid.innerHTML = filtered.map((j) => `
    <div class="job-card">
      <div class="top-row">
        <div>
          <p class="title">${j.title}</p>
          <p class="company">${j.company}</p>
        </div>
        <span class="source-badge ${j.is_open_market ? "open-market" : "scheme"}">${j.is_open_market ? j.source : (j.source === "company_posted" ? "company" : "scheme")}</span>
      </div>
      <div class="meta">${j.location}${j.total_capacity > 1 ? ` · ${j.total_capacity} seats` : ""}</div>
      <div class="skill-tags">${(j.required_skills || []).slice(0, 6).map((s) => `<span class="skill-tag">${s}</span>`).join("")}</div>
      ${j.apply_url ? `<a class="apply-link" href="${j.apply_url}" target="_blank" rel="noopener">View original posting →</a>` : ""}
    </div>
  `).join("");
}

// --- admin: manual scheme internship ---
const manualBtn = document.getElementById("manual-submit-btn");
if (manualBtn) withLoading(manualBtn, async () => {
  const resultEl = document.getElementById("manual-result");
  const title = document.getElementById("manual-title").value.trim();
  const company = document.getElementById("manual-company").value.trim();
  const location = document.getElementById("manual-location").value.trim();
  const capacity = Number(document.getElementById("manual-capacity").value);
  if (!title || !company || !location) {
    resultEl.textContent = "Title, company, and location are required.";
    return;
  }
  if (!Number.isFinite(capacity) || capacity < 1) {
    resultEl.textContent = "Total capacity must be a whole number of 1 or more.";
    return;
  }
  try {
    const skills = document.getElementById("manual-skills").value.split(",").map((s) => s.trim()).filter(Boolean);
    await api("/api/internships/manual", {
      method: "POST",
      json: { title, company, required_skills: skills, location, total_capacity: capacity },
    });
    resultEl.textContent = "Added and matching applicants notified.";
    document.getElementById("manual-title").value = "";
    document.getElementById("manual-company").value = "";
    document.getElementById("manual-skills").value = "";
    document.getElementById("manual-location").value = "";
    await loadJobs();
  } catch (err) {
    resultEl.textContent = "Error: " + err.message;
  }
});

// --- admin: Greenhouse / Lever ingestion ---
const ingestBtn = document.getElementById("ingest-submit-btn");
if (ingestBtn) withLoading(ingestBtn, async () => {
  const resultEl = document.getElementById("ingest-result");
  const identifier = document.getElementById("ingest-identifier").value.trim();
  if (!identifier) {
    resultEl.textContent = "Enter a board token / company slug first (e.g. \"stripe\").";
    return;
  }
  resultEl.textContent = "Fetching…";
  try {
    const result = await api("/api/internships/ingest", {
      method: "POST",
      json: { source: document.getElementById("ingest-source").value, identifier },
    });
    resultEl.textContent = `Fetched ${result.postings_fetched}, added ${result.new_postings} new, sent ${result.notifications_sent} notification(s).`;
    await loadJobs();
  } catch (err) {
    resultEl.textContent = "Error: " + err.message + " — if this venue's network blocks outbound calls, use \"Paste a posting\" instead.";
  }
});

// --- admin: paste-and-parse ---
const pasteBtn = document.getElementById("paste-submit-btn");
if (pasteBtn) withLoading(pasteBtn, async () => {
  const resultEl = document.getElementById("paste-result");
  const rawText = document.getElementById("paste-text").value.trim();
  const company = document.getElementById("paste-company").value.trim();
  if (!rawText || !company) {
    resultEl.textContent = "Company name and posting text are both required.";
    return;
  }
  try {
    const result = await api("/api/internships/paste", {
      method: "POST",
      json: {
        raw_text: rawText,
        company,
        location: document.getElementById("paste-location").value || "Not specified",
      },
    });
    resultEl.textContent = `Added ${result.new_postings} posting, sent ${result.notifications_sent} notification(s).`;
    document.getElementById("paste-text").value = "";
    await loadJobs();
  } catch (err) {
    resultEl.textContent = "Error: " + err.message;
  }
});

// ===========================================================================
// Notifications
// ===========================================================================

async function loadNotifications() {
  state.notifications = await api("/api/notifications/me");
  renderNotifications();
}

function renderNotifications() {
  const unread = state.notifications.filter((n) => !n.read_at).length;
  const badge = document.getElementById("unread-badge");
  if (unread > 0) {
    badge.style.display = "inline-block";
    badge.textContent = unread;
  } else {
    badge.style.display = "none";
  }

  const list = document.getElementById("notif-list");
  if (!state.notifications.length) {
    list.innerHTML = `<div class="empty-state"><h3>No notifications yet</h3><p>Upload your resume and matches will appear here automatically.</p></div>`;
    return;
  }

  list.innerHTML = state.notifications.map((n) => `
    <div class="notif-row ${n.read_at ? "" : "unread"}">
      <div class="msg">
        ${n.read_at ? "" : '<span class="unread-dot"></span>'}${n.message}
        <span class="when">${new Date(n.created_at).toLocaleString()}</span>
      </div>
      ${n.read_at ? "" : `<button data-id="${n.id}" class="mark-read-btn">Mark read</button>`}
    </div>
  `).join("");

  list.querySelectorAll(".mark-read-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await api(`/api/notifications/${btn.dataset.id}/read`, { method: "POST" });
      await loadNotifications();
    });
  });
}

// ===========================================================================
// Allocation console (admin only)
// ===========================================================================

withLoading(document.getElementById("seed-btn"), async () => {
  const statusEl = document.getElementById("alloc-status-line");
  statusEl.textContent = "Adding demo scheme internships…";
  try {
    const result = await api("/api/seed-internships", { method: "POST" });
    statusEl.textContent = `Added ${result.internships_created} demo scheme internships.`;
    setPipelineStage("collect");
    await loadJobs();
  } catch (err) {
    statusEl.textContent = "Error: " + err.message;
  }
});

withLoading(document.getElementById("allocate-btn"), async () => {
  const statusEl = document.getElementById("alloc-status-line");
  statusEl.textContent = "Scoring candidates and running deferred acceptance (no interview stage)…";
  setPipelineStage("score");
  try {
    const summary = await api("/api/allocate", { method: "POST" });
    const violationNote = summary.stability_violations.length
      ? ` — WARNING: ${summary.stability_violations.length} stability violation(s)`
      : " — stability verified";
    statusEl.textContent = `Run ${summary.run_id.slice(0, 8)}: ${summary.allocated} allocated, ${summary.unallocated} unallocated${violationNote}`;

    // A quick allocate bypasses the interview stage entirely — clear any
    // stale in-progress cycle UI so it isn't confused with this result.
    state.pipeline = { cycleId: null, interviews: [] };
    document.getElementById("cycle-status-line").textContent = "No cycle prepared yet.";
    document.getElementById("finalize-cycle-btn").disabled = true;
    document.getElementById("interview-body").innerHTML = "";
    setPipelineStage("finalize");

    await refreshAllocationView(summary.run_id);
  } catch (err) {
    statusEl.textContent = "Error: " + err.message;
    setPipelineStage("collect");
  }
});

withLoading(document.getElementById("reset-demo-btn"), async () => {
  const statusEl = document.getElementById("alloc-status-line");
  const confirmed = confirm(
    "This clears all applicants, internships, allocations, interviews, and notifications " +
    "so the app can be demoed again from a clean state. Registered logins are kept. Continue?"
  );
  if (!confirmed) return;
  statusEl.textContent = "Resetting demo data…";
  try {
    const result = await api("/api/admin/reset-demo", { method: "POST" });
    statusEl.textContent = result.message;
    state.pipeline = { cycleId: null, interviews: [] };
    state.allocation = { internships: [], allocations: [], fairness: null };
    document.getElementById("cycle-status-line").textContent = "No cycle prepared yet.";
    document.getElementById("finalize-cycle-btn").disabled = true;
    document.getElementById("interview-body").innerHTML = "";
    document.getElementById("alloc-body").innerHTML = `<div class="empty-state"><h3>No allocation run yet</h3><p>Add demo internships and run a cycle to start again.</p></div>`;
    setPipelineStage("collect");
    await loadJobs();
    await loadNotifications();
  } catch (err) {
    statusEl.textContent = "Error: " + err.message;
  }
});

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

async function refreshAllocationView(runIdOrCycleId) {
  const [internships, allocations, fairnessReport] = await Promise.all([
    api("/api/internships"),
    api("/api/allocations/latest"),
    api("/api/fairness/latest"),
  ]);
  state.allocation = {
    internships: internships.filter((i) => !i.is_open_market),
    allocations,
    fairness: fairnessReport,
  };
  renderAllocation();
}

// --- Stage 1: prepare a cycle (score every pair, provisional match, select interviews) ---
withLoading(document.getElementById("prepare-cycle-btn"), async () => {
  const statusEl = document.getElementById("cycle-status-line");
  const maxInterviewsInput = document.getElementById("max-interviews-input");
  const maxInterviews = Number(maxInterviewsInput.value);
  if (!Number.isFinite(maxInterviews) || maxInterviews < 1) {
    statusEl.textContent = "Max interviews must be a whole number of 1 or more.";
    return;
  }
  statusEl.textContent = "Scoring every pair and selecting contested interviews…";
  setPipelineStage("score");
  try {
    const url = `/api/allocate/prepare?max_interviews=${encodeURIComponent(maxInterviews)}`;
    const result = await api(url, { method: "POST" });
    state.pipeline.cycleId = result.cycle_id;
    statusEl.textContent = `Cycle ${result.cycle_id.slice(0, 8)}: ${result.provisional_allocated} provisionally allocated, `
      + `${result.provisional_unallocated} not yet placed, ${result.interviews_selected} interview(s) selected as worth holding.`;
    document.getElementById("finalize-cycle-btn").disabled = false;
    setPipelineStage("interview");
    await loadInterviews();
  } catch (err) {
    statusEl.textContent = "Error: " + err.message;
    setPipelineStage("collect");
  }
});

async function loadInterviews() {
  if (!state.pipeline.cycleId) return;
  state.pipeline.interviews = await api(`/api/interviews?cycle_id=${encodeURIComponent(state.pipeline.cycleId)}`);
  renderInterviews();
}

document.getElementById("company-filter-input").addEventListener("input", (e) => {
  state.companyFilter = e.target.value.trim().toLowerCase();
  renderInterviews();
  if (state.allocation.allocations.length) renderAllocTable(state.allocation.allocations);
});

function renderInterviews() {
  const body = document.getElementById("interview-body");
  const allInterviews = state.pipeline.interviews;
  const interviews = state.companyFilter
    ? allInterviews.filter((iv) => (iv.company || "").toLowerCase().includes(state.companyFilter))
    : allInterviews;

  if (!state.pipeline.cycleId) {
    body.innerHTML = "";
    return;
  }

  if (!allInterviews.length) {
    body.innerHTML = `<div class="panel" style="padding:16px;"><p class="section-note" style="margin:0;">No pairs were contested enough to need an interview this cycle — every capacity bucket either has free seats or the AI is already confident. You can finalize directly.</p></div>`;
    return;
  }

  if (!interviews.length) {
    body.innerHTML = `<div class="panel" style="padding:16px;"><p class="section-note" style="margin:0;">No interviews match "${esc(state.companyFilter)}" for this company. Clear the filter to see all ${allInterviews.length}.</p></div>`;
    return;
  }

  body.innerHTML = `
    <h3 style="margin:18px 0 10px;">Interviews worth holding (${interviews.length}${interviews.length !== allInterviews.length ? ` of ${allInterviews.length}` : ""})</h3>
    <div class="interview-list" id="interview-list"></div>
  `;

  const list = document.getElementById("interview-list");
  list.innerHTML = interviews.map((iv) => `
    <div class="interview-card ${iv.status === "COMPLETED" ? "completed" : ""}" data-id="${iv.id}">
      <div class="top-row">
        <span class="who">${esc(iv.applicant_name)}</span>
        <span class="what">${esc(iv.internship_title)} · ${esc(iv.company)} (${esc(iv.bucket)} seat)</span>
      </div>
      <div class="metrics">
        <span>AI score: ${iv.ai_score.toFixed(2)}</span>
        <span>Confidence: ${(iv.confidence * 100).toFixed(0)}%</span>
        <span>Margin from cutoff: ${iv.margin.toFixed(2)}</span>
      </div>
      <p class="reason">${esc(iv.reason)}</p>
      ${iv.status === "COMPLETED"
        ? `<p class="outcome-recorded">Outcome recorded: ${iv.outcome_score.toFixed(2)}${iv.notes ? " — " + esc(iv.notes) : ""}</p>`
        : `<div class="outcome-row">
             <input type="number" min="0" max="1" step="0.01" placeholder="Outcome 0–1" class="outcome-score-input" />
             <input type="text" placeholder="Notes (optional)" class="outcome-notes-input" />
             <button class="record-outcome-btn" data-id="${iv.id}">Record outcome</button>
           </div>`
      }
    </div>
  `).join("");

  list.querySelectorAll(".outcome-score-input").forEach((input) => {
    input.addEventListener("input", () => input.classList.remove("input-error"));
  });

  list.querySelectorAll(".record-outcome-btn").forEach((btn) => {
    withLoading(btn, async () => {
      const card = btn.closest(".interview-card");
      const scoreInput = card.querySelector(".outcome-score-input");
      const notesInput = card.querySelector(".outcome-notes-input");
      const outcomeScore = Number(scoreInput.value);
      if (scoreInput.value.trim() === "" || Number.isNaN(outcomeScore) || outcomeScore < 0 || outcomeScore > 1) {
        scoreInput.classList.add("input-error");
        scoreInput.focus();
        return;
      }
      try {
        await api(`/api/interviews/${btn.dataset.id}/outcome`, {
          method: "POST",
          json: { outcome_score: outcomeScore, notes: notesInput.value || null },
        });
        await loadInterviews();
      } catch (err) {
        alert("Error: " + err.message);
      }
    });
  });
}

// --- Stage 4: finalize the cycle (final stable match using interview outcomes where recorded) ---
withLoading(document.getElementById("finalize-cycle-btn"), async () => {
  const statusEl = document.getElementById("alloc-status-line");
  if (!state.pipeline.cycleId) return;
  const pending = state.pipeline.interviews.filter((iv) => iv.status !== "COMPLETED").length;
  if (pending > 0) {
    const proceed = confirm(
      `${pending} selected interview(s) don't have a recorded outcome yet. Finalizing now will use the ` +
      `original AI-only score for those pairs, same as if no interview had been selected. Continue?`
    );
    if (!proceed) return;
  }
  statusEl.textContent = "Running the final stable match…";
  setPipelineStage("finalize");
  try {
    const url = `/api/allocate/finalize?cycle_id=${encodeURIComponent(state.pipeline.cycleId)}`;
    const summary = await api(url, { method: "POST" });
    const violationNote = summary.stability_violations.length
      ? ` — WARNING: ${summary.stability_violations.length} stability violation(s)`
      : " — stability verified";
    statusEl.textContent = `Finalized ${summary.cycle_id.slice(0, 8)}: ${summary.allocated} allocated, `
      + `${summary.unallocated} unallocated, ${summary.interviews_completed}/${summary.interviews_used} interview(s) used${violationNote}`;
    await refreshAllocationView(summary.run_id);
  } catch (err) {
    statusEl.textContent = "Error: " + err.message;
  }
});

function renderAllocation() {
  const { internships, allocations, fairness } = state.allocation;
  const body = document.getElementById("alloc-body");
  if (!allocations.length) {
    body.innerHTML = `<div class="empty-state"><h3>No allocations yet</h3></div>`;
    return;
  }

  const allocatedCount = allocations.filter((a) => a.internship_id !== null).length;
  const unallocatedCount = allocations.length - allocatedCount;
  const ratio = fairness.four_fifths_rule_ratio;

  body.innerHTML = `
    <div class="stats">
      <div class="stat-card"><p class="label">Applicants</p><p class="value">${allocations.length}</p></div>
      <div class="stat-card"><p class="label">Allocated</p><p class="value good">${allocatedCount}</p></div>
      <div class="stat-card"><p class="label">Unallocated</p><p class="value ${unallocatedCount > 0 ? "warn" : ""}">${unallocatedCount}</p></div>
      <div class="stat-card"><p class="label">Allocation rate</p><p class="value">${(fairness.overall_allocation_rate * 100).toFixed(1)}%</p></div>
      <div class="stat-card"><p class="label">4/5ths ratio</p><p class="value ${ratio < 0.8 ? "warn" : "good"}">${ratio.toFixed(2)}</p></div>
    </div>

    <section>
      <h2>Seat map</h2>
      <p class="section-note">Teal = open-merit fill, coral = reservation-quota fill, outlined = unfilled.</p>
      <div class="panel">
        <div class="seat-map-list" id="seat-map"></div>
        <div class="legend">
          <span><span class="seat open-filled"></span> Open merit</span>
          <span><span class="seat quota-filled"></span> Reservation quota</span>
          <span><span class="seat empty"></span> Unfilled</span>
        </div>
      </div>
    </section>

    <section>
      <h2>Fairness by category</h2>
      <div class="panel">
        <div id="fairness-bars"></div>
        <div class="ratio-flag ${ratio < 0.8 ? "warn" : "ok"}">
          ${ratio < 0.8
            ? `Four-fifths ratio is ${ratio.toFixed(2)} (below 0.8) — flagged for human review.`
            : `Four-fifths ratio is ${ratio.toFixed(2)} — no disparity flagged.`}
        </div>
      </div>
    </section>

    <section>
      <h2>Score distribution</h2>
      <div class="panel">
        <div id="score-histogram"></div>
      </div>
    </section>

    <section>
      <h2>Allocations</h2>
      <div class="panel">
        <table>
          <thead><tr><th>Applicant</th><th>Category</th><th>Internship</th><th>Seat</th><th>Score</th></tr></thead>
          <tbody id="alloc-tbody"></tbody>
        </table>
      </div>
    </section>
  `;

  renderSeatMap(internships, allocations);
  renderFairnessBars(fairness);
  renderScoreHistogram(allocations);
  renderAllocTable(allocations);
}

function renderScoreHistogram(allocations) {
  const container = document.getElementById("score-histogram");
  const buckets = [
    { label: "0.0–0.2", min: 0, max: 0.2, count: 0 },
    { label: "0.2–0.4", min: 0.2, max: 0.4, count: 0 },
    { label: "0.4–0.6", min: 0.4, max: 0.6, count: 0 },
    { label: "0.6–0.8", min: 0.6, max: 0.8, count: 0 },
    { label: "0.8–1.0", min: 0.8, max: 1.01, count: 0 },
  ];
  let scored = 0;
  for (const a of allocations) {
    if (a.score === null || a.score === undefined) continue;
    scored++;
    const bucket = buckets.find((b) => a.score >= b.min && a.score < b.max);
    if (bucket) bucket.count++;
  }
  if (!scored) {
    container.innerHTML = `<p class="section-note" style="margin:0;">No scored allocations yet.</p>`;
    return;
  }
  const maxCount = Math.max(...buckets.map((b) => b.count), 1);
  container.innerHTML = buckets.map((b) => `
    <div class="hist-row">
      <span class="bucket">${b.label}</span>
      <div class="bar-track"><div class="bar-fill" style="width:${(b.count / maxCount * 100).toFixed(1)}%"></div></div>
      <span class="count">${b.count}</span>
    </div>`).join("");
}

function renderSeatMap(internships, allocations) {
  const container = document.getElementById("seat-map");
  const occupancy = {};
  for (const a of allocations) {
    if (a.internship_id === null) continue;
    occupancy[a.internship_id] = occupancy[a.internship_id] || [];
    occupancy[a.internship_id].push(a.seat_type);
  }
  container.innerHTML = internships.map((i) => {
    const filled = occupancy[i.id] || [];
    const seats = filled.map((t) => `<div class="seat ${t === "OPEN" ? "open-filled" : "quota-filled"}" title="${t}"></div>`);
    for (let n = filled.length; n < i.total_capacity; n++) seats.push(`<div class="seat empty"></div>`);
    return `
      <div class="seat-map-row">
        <div><div class="title">${i.title}</div><div class="company">${i.company} · ${i.location}</div></div>
        <div class="seats">${seats.join("")}</div>
      </div>`;
  }).join("");
}

function renderFairnessBars(fairness) {
  const container = document.getElementById("fairness-bars");
  const maxRate = Math.max(...fairness.by_category.map((c) => c.allocation_rate), 0.01);
  container.innerHTML = fairness.by_category.map((c) => `
    <div class="fairness-row">
      <div class="cat">${c.category}</div>
      <div class="bar-track"><div class="bar-fill" style="width:${(c.allocation_rate / maxRate * 100).toFixed(1)}%"></div></div>
      <div class="rate">${(c.allocation_rate * 100).toFixed(1)}% (${c.allocated}/${c.total_applicants})</div>
    </div>`).join("");
}

function renderAllocTable(allocations) {
  const tbody = document.getElementById("alloc-tbody");
  const filtered = state.companyFilter
    ? allocations.filter((a) => (a.company || "").toLowerCase().includes(state.companyFilter))
    : allocations;

  const note = document.getElementById("alloc-filter-note");
  if (note) note.remove();
  if (state.companyFilter) {
    const table = tbody.closest("table");
    const p = document.createElement("p");
    p.id = "alloc-filter-note";
    p.className = "section-note";
    p.style.margin = "0 0 8px";
    p.textContent = `Showing ${filtered.length} of ${allocations.length} allocations matching "${state.companyFilter}".`;
    table.parentElement.insertBefore(p, table);
  }

  if (!filtered.length) {
    tbody.innerHTML = `<tr><td colspan="5" style="text-align:center; color:var(--ink-faint); padding:18px;">No allocations match this company filter.</td></tr>`;
    return;
  }

  tbody.innerHTML = filtered.map((a, idx) => {
    const seatBadge = a.internship_id === null
      ? `<span class="badge none">Unallocated</span>`
      : a.seat_type === "OPEN" ? `<span class="badge open">Open merit</span>` : `<span class="badge quota">${a.seat_type} quota</span>`;
    const internshipLabel = a.internship_id === null ? "—" : `${a.internship_title}<div style="color:var(--ink-faint); font-size:11.5px;">${a.company}</div>`;
    const scoreLabel = a.score === null ? "—" : a.score.toFixed(2);
    const rowId = `explain-${idx}`;
    const components = a.explanation && a.explanation.components ? Object.entries(a.explanation.components) : [];

    return `
      <tr class="${a.internship_id === null ? "unallocated" : ""}">
        <td>
          ${a.applicant_name}<br/>
          <button class="explain-toggle" data-target="${rowId}">Show reasoning</button>
          <div class="explain-panel" id="${rowId}">
            ${a.ai_summary ? `<p class="ai-summary">✦ ${esc(a.ai_summary)}</p>` : ""}
            <p class="narrative">${a.explanation ? a.explanation.narrative : ""}</p>
            ${a.explanation && a.explanation.interview_note ? `<p class="narrative" style="color:var(--coral);">${esc(a.explanation.interview_note)}</p>` : ""}
            ${components.map(([name, c]) => `
              <div class="component-row">
                <span class="name">${name.replace("_", " ")}</span>
                <span>${c.detail}</span>
                <span class="contrib">${c.contribution.toFixed(2)}</span>
              </div>`).join("")}
          </div>
        </td>
        <td>${a.category}</td>
        <td>${internshipLabel}</td>
        <td>${seatBadge}</td>
        <td class="score">${scoreLabel}</td>
      </tr>`;
  }).join("");

  tbody.querySelectorAll(".explain-toggle").forEach((btn) => {
    btn.addEventListener("click", () => {
      const panel = document.getElementById(btn.dataset.target);
      const shown = panel.classList.toggle("shown");
      btn.textContent = shown ? "Hide reasoning" : "Show reasoning";
    });
  });
}

// ===========================================================================
// Company console (company role only)
// ===========================================================================

withLoading(document.getElementById("company-post-btn"), async () => {
  const resultEl = document.getElementById("company-post-result");
  const title = document.getElementById("company-post-title").value.trim();
  const skills = document.getElementById("company-post-skills").value.split(",").map((s) => s.trim()).filter(Boolean);
  const location = document.getElementById("company-post-location").value.trim();
  const capacity = Number(document.getElementById("company-post-capacity").value);

  if (!title || !location) {
    resultEl.textContent = "Title and location are required.";
    return;
  }
  if (!Number.isFinite(capacity) || capacity < 1) {
    resultEl.textContent = "Total seats must be a whole number of 1 or more.";
    return;
  }
  try {
    await api("/api/company/internships", {
      method: "POST",
      json: { title, required_skills: skills, location, total_capacity: capacity },
    });
    resultEl.textContent = "Posted. Matching applicants have been notified.";
    document.getElementById("company-post-title").value = "";
    document.getElementById("company-post-skills").value = "";
    document.getElementById("company-post-location").value = "";
    document.getElementById("company-post-capacity").value = "1";
    await loadJobs();
    await loadCompanyConsole();
  } catch (err) {
    resultEl.textContent = "Error: " + err.message;
  }
});

document.getElementById("company-refresh-btn").addEventListener("click", loadCompanyConsole);

async function loadCompanyConsole() {
  const [internships, interviews, allocations] = await Promise.all([
    api("/api/company/internships"),
    api("/api/company/interviews").catch(() => []),
    api("/api/company/allocations").catch(() => []),
  ]);
  renderCompanyInternships(internships);
  renderCompanyInterviews(interviews);
  renderCompanyAllocations(allocations);
}

function renderCompanyInternships(internships) {
  const body = document.getElementById("company-internships-body");
  if (!internships.length) {
    body.innerHTML = `<div class="empty-state"><h3>No postings yet</h3><p>Post your first opening above.</p></div>`;
    return;
  }
  body.innerHTML = `
    <table>
      <thead><tr><th>Title</th><th>Location</th><th>Seats</th><th>Required skills</th><th>Posted</th></tr></thead>
      <tbody>
        ${internships.map((i) => `
          <tr>
            <td>${esc(i.title)}</td>
            <td>${esc(i.location)}</td>
            <td>${i.total_capacity}</td>
            <td>${(i.required_skills || []).map((s) => `<span class="skill-tag">${esc(s)}</span>`).join(" ")}</td>
            <td style="font-family:var(--font-mono); font-size:11.5px; color:var(--ink-faint);">${new Date(i.posted_at).toLocaleDateString()}</td>
          </tr>`).join("")}
      </tbody>
    </table>`;
}

function renderCompanyInterviews(interviews) {
  const body = document.getElementById("company-interviews-body");
  if (!interviews.length) {
    body.innerHTML = `<p class="section-note" style="margin:0;">No interviews selected for your postings yet. This fills in once the placement team runs "Prepare cycle" and the AI finds a contested pair on one of your openings.</p>`;
    return;
  }
  body.innerHTML = `<div class="interview-list" id="company-interview-list"></div>`;
  const list = document.getElementById("company-interview-list");
  list.innerHTML = interviews.map((iv) => `
    <div class="interview-card ${iv.status === "COMPLETED" ? "completed" : ""}" data-id="${iv.id}">
      <div class="top-row">
        <span class="who">${esc(iv.applicant_name)}</span>
        <span class="what">${esc(iv.internship_title)} (${esc(iv.bucket)} seat)</span>
      </div>
      <div class="metrics">
        <span>AI score: ${iv.ai_score.toFixed(2)}</span>
        <span>Confidence: ${(iv.confidence * 100).toFixed(0)}%</span>
      </div>
      <p class="reason">${esc(iv.reason)}</p>
      ${iv.status === "COMPLETED"
        ? `<p class="outcome-recorded">Outcome recorded: ${iv.outcome_score.toFixed(2)}${iv.notes ? " — " + esc(iv.notes) : ""}</p>`
        : `<div class="outcome-row">
             <input type="number" min="0" max="1" step="0.01" placeholder="Outcome 0–1" class="outcome-score-input" />
             <input type="text" placeholder="Notes (optional)" class="outcome-notes-input" />
             <button class="record-outcome-btn company-record-btn" data-id="${iv.id}">Record outcome</button>
           </div>`
      }
    </div>
  `).join("");

  list.querySelectorAll(".outcome-score-input").forEach((input) => {
    input.addEventListener("input", () => input.classList.remove("input-error"));
  });

  list.querySelectorAll(".company-record-btn").forEach((btn) => {
    withLoading(btn, async () => {
      const card = btn.closest(".interview-card");
      const scoreInput = card.querySelector(".outcome-score-input");
      const notesInput = card.querySelector(".outcome-notes-input");
      const outcomeScore = Number(scoreInput.value);
      if (scoreInput.value.trim() === "" || Number.isNaN(outcomeScore) || outcomeScore < 0 || outcomeScore > 1) {
        scoreInput.classList.add("input-error");
        scoreInput.focus();
        return;
      }
      try {
        await api(`/api/interviews/${btn.dataset.id}/outcome`, {
          method: "POST",
          json: { outcome_score: outcomeScore, notes: notesInput.value || null },
        });
        await loadCompanyConsole();
      } catch (err) {
        alert("Error: " + err.message);
      }
    });
  });
}

function renderCompanyAllocations(allocations) {
  const body = document.getElementById("company-allocations-body");
  if (!allocations.length) {
    body.innerHTML = `<p class="section-note" style="margin:0;">No finalized allocations for your postings yet.</p>`;
    return;
  }
  body.innerHTML = `
    <table>
      <thead><tr><th>Applicant</th><th>Internship</th><th>Seat</th><th>Score</th></tr></thead>
      <tbody>
        ${allocations.map((a) => `
          <tr>
            <td>${esc(a.applicant_name)}</td>
            <td>${esc(a.internship_title)}</td>
            <td><span class="badge open">${esc(a.seat_type)}</span></td>
            <td class="score">${a.score !== null ? a.score.toFixed(2) : "—"}</td>
          </tr>`).join("")}
      </tbody>
    </table>`;
}
