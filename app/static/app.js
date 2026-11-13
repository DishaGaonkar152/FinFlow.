const state = { run: null, selectedMetric: null, decision: null, chat: [], assistantOpen: false };
const $ = (id) => document.getElementById(id);
let csrfToken = null;
const narration = { scope: null, messageScope: null, runId: null, snapshot: null, parts: [], index: 0, utterance: null, message: "" };

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = text;
  return element;
}

function speechAvailable() {
  return typeof window.SpeechSynthesisUtterance === "function" && typeof window.speechSynthesis?.speak === "function";
}

function narrationMetrics(scope) {
  const metrics = state.run?.state.metrics || [];
  return metrics.filter((metric) => metric.explanation && (scope === "report" || metric.key === state.selectedMetric?.key));
}

function speechParts(metric) {
  const explanation = metric.explanation;
  return [
    `${metric.label}. Current value: ${metric.value === null ? "Unavailable" : formatValue(metric.value, metric.unit)}. Current status: ${statusLabel(metric.status)}. ${explanation.generated_by === "llm" ? "AI-generated explanation" : "Rule-based fallback explanation"}.`,
    explanation.summary,
    explanation.extraction_reason ? `Why this value was extracted. ${explanation.extraction_reason}` : "",
    explanation.possible_causes?.length ? `Possible causes, not confirmed facts. ${explanation.possible_causes.join(". ")}` : "",
    explanation.review_guidance ? `Reviewer guidance. ${explanation.review_guidance}` : "",
  ].filter(Boolean);
}

function splitSpeech(text) {
  const parts = [];
  let remaining = text.replace(/\s+/g, " ").trim();
  while (remaining.length > 240) {
    const space = remaining.lastIndexOf(" ", 240);
    const end = space > 0 ? space : 240;
    parts.push(remaining.slice(0, end));
    remaining = remaining.slice(end).trim();
  }
  if (remaining) parts.push(remaining);
  return parts;
}

function renderSpeechControls() {
  const supported = speechAvailable();
  for (const controls of document.querySelectorAll(".speech-controls")) {
    const scope = controls.dataset.speechScope;
    const active = Boolean(narration.utterance && narration.scope === scope);
    const available = narrationMetrics(scope).length > 0;
    const start = controls.querySelector(".speech-start");
    start.disabled = !supported || !available || active;
    start.textContent = active ? "Reading aloud…" : scope === "report" ? "Read explanations aloud" : "Read this explanation";
    controls.querySelector(".speech-stop").disabled = !narration.utterance;
    controls.querySelector(".speech-status").textContent = !supported
      ? "Read aloud is not supported in this browser. Try Chrome or Edge."
      : !available
        ? "No saved explanation is available to read."
        : (narration.scope === scope || narration.messageScope === scope) && narration.message
          ? narration.message
          : "Reads saved AI or rule-based explanations using your browser voice. No new AI request is made.";
  }
}

function stopNarration(message = "") {
  const scope = narration.scope;
  narration.utterance = null;
  narration.parts = [];
  narration.index = 0;
  narration.scope = null;
  narration.messageScope = message ? scope : null;
  narration.runId = null;
  narration.snapshot = null;
  narration.message = message;
  if (speechAvailable()) window.speechSynthesis.cancel();
  renderSpeechControls();
}

function speakNext() {
  if (narration.index >= narration.parts.length) {
    stopNarration("Finished reading.");
    return;
  }
  const utterance = new window.SpeechSynthesisUtterance(narration.parts[narration.index]);
  const voices = window.speechSynthesis.getVoices();
  const voice = voices.find((item) => item.localService && /^en\b/i.test(item.lang)) || voices.find((item) => item.localService);
  if (voice) utterance.voice = voice;
  utterance.lang = voice?.lang || "en-US";
  utterance.rate = 1;
  narration.utterance = utterance;
  narration.message = narration.scope === "report" ? "Reading saved report explanations…" : "Reading this metric explanation…";
  utterance.onend = () => {
    if (narration.utterance !== utterance) return;
    narration.index += 1;
    speakNext();
  };
  utterance.onerror = () => {
    if (narration.utterance !== utterance) return;
    stopNarration("Speech could not play. Check your browser voice settings, then try again.");
  };
  renderSpeechControls();
  try {
    window.speechSynthesis.speak(utterance);
  } catch (error) {
    stopNarration("Speech could not start. Check your browser voice settings, then try again.");
  }
}

function startNarration(scope) {
  const metrics = narrationMetrics(scope);
  if (!speechAvailable() || !metrics.length) return;
  stopNarration();
  narration.scope = scope;
  narration.runId = state.run.id;
  narration.snapshot = JSON.stringify(state.run.state.metrics);
  const opening = scope === "report"
    ? [`${state.run.state.company.name}. ${statusLabel(state.run.status)} financial audit. Saved explanations are based on initial extraction and were not regenerated after review decisions.`]
    : ["This saved explanation is based on initial extraction and was not regenerated after review decisions."];
  narration.parts = [...opening, ...metrics.flatMap(speechParts)].flatMap(splitSpeech);
  speakNext();
}

function speechControls(scope) {
  const controls = node("div", "speech-controls");
  controls.dataset.speechScope = scope;
  const start = node("button", "button button-secondary speech-start");
  start.type = "button";
  start.addEventListener("click", () => startNarration(scope));
  const stop = node("button", "button button-secondary speech-stop", "Stop reading");
  stop.type = "button";
  stop.addEventListener("click", () => stopNarration());
  const status = node("span", "speech-status");
  status.setAttribute("role", "status");
  status.setAttribute("aria-live", "polite");
  controls.append(start, stop, status);
  return controls;
}

function formatValue(value, unit = "USD") {
  if (value === null || value === undefined) return "—";
  if (unit === "USD/shares") return `$${Number(value).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 4 })}`;
  return Number(value).toLocaleString(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 0 });
}

function formatDate(value) {
  if (!value) return "—";
  return new Date(value).toLocaleString();
}

function statusLabel(status) {
  return (status || "unknown").replaceAll("_", " ");
}

function toast(message) {
  $("toast").textContent = message;
  $("toast").hidden = false;
  window.clearTimeout(toast.timer);
  toast.timer = window.setTimeout(() => { $("toast").hidden = true; }, 4000);
}

function appendChatMessage(role, text, attribution = null, record = true) {
  const message = node("div", `chat-message ${role}`);
  message.append(node("div", "chat-bubble", text));
  if (attribution) message.append(node("small", "chat-attribution", attribution));
  $("chat-messages").append(message);
  $("chat-messages").scrollTop = $("chat-messages").scrollHeight;
  if (record) state.chat.push({ role, content: text });
  return message;
}

function resetAssistant() {
  state.chat = [];
  $("chat-messages").replaceChildren();
  appendChatMessage(
    "assistant",
    "Ask me about this dashboard. For example: ‘Why does total liabilities need review?’, ‘What is the XBRL accuracy?’, or ‘Show the latest audit decision.’",
    null,
    false,
  );
}

function setAssistantOpen(open) {
  if (open) setOrchestrationOpen(false);
  state.assistantOpen = open;
  $("assistant-panel").hidden = !open;
  $("assistant-toggle").setAttribute("aria-expanded", String(open));
  if (open) $("chat-input").focus();
}

function chatAttribution(result) {
  return result.generated_by === "llm"
    ? `AI-generated · ${result.model}`
    : "Rule-based fallback · deterministic dashboard answer";
}

function requireSignIn(response) {
  if (response.status === 401) {
    stopNarration();
    window.location.replace("/login");
    throw new Error("Your session ended. Please sign in again.");
  }
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(csrfToken ? { "X-CSRF-Token": csrfToken } : {}), ...(options.headers || {}) },
  });
  requireSignIn(response);
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || "Request failed");
  return data;
}

const companyPicker = { ready: false, loading: false, running: false, selected: null, searchSequence: 0, detailSequence: 0 };

function companyCIK(value) {
  const text = String(value ?? "").trim().replace(/^CIK[\s:-]*/i, "");
  return /^\d{1,10}$/.test(text) && Number(text) > 0 ? text.padStart(10, "0") : null;
}

function demoCompany(company) {
  return company?.ticker === "DEMO" || company?.cik === "DEMO";
}

function companyLabel(company) {
  return company?.ticker || (companyCIK(company?.cik) ? `CIK ${companyCIK(company.cik)}` : company?.name || "Company unavailable");
}

function secURL(value) {
  if (typeof value !== "string" || !value.startsWith("https://")) return null;
  try {
    const url = new URL(value);
    return ["sec.gov", "www.sec.gov", "data.sec.gov"].includes(url.hostname) && !url.username && !url.password && !url.port ? url.href : null;
  } catch (error) { return null; }
}

function appendSECLink(container, value, label, company) {
  const href = demoCompany(company) ? null : secURL(value);
  if (!href) return;
  const link = node("a", "company-source-link", label);
  link.href = href;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  container.append(link);
}

function renderCompanyDetails(container, company, saved = false) {
  container.replaceChildren();
  if (!company) {
    container.append(node("p", "", "Company details are unavailable."));
    return;
  }
  const demo = demoCompany(company);
  container.append(node("h4", "", company.name || "Company name unavailable"));
  if (saved) container.append(node("p", "company-detail-note", "Saved audit snapshot, not a live registry lookup."));
  const fields = node("dl", "company-detail-grid");
  for (const [label, value] of [
    ["Ticker", company.ticker || (demo ? "DEMO" : "No listed ticker")],
    ["CIK", demo ? "Not applicable (offline DEMO)" : companyCIK(company.cik) || "Not saved / unavailable"],
    ["Exchanges", Array.isArray(company.exchanges) ? company.exchanges.join(", ") || "None listed" : company.exchange],
    ["Industry", company.industry], ["SIC", company.sic],
    ["Entity type", company.entity_type], ["Fiscal year end", company.fiscal_year_end],
    ["Address", typeof company.address === "string" ? company.address : null],
  ]) fields.append(node("dt", "", label), node("dd", "", value === undefined || value === null || value === "" ? "Not saved / unavailable" : String(value)));
  container.append(fields);
  if (company.description) container.append(node("p", "company-description", company.description));
  const available = demo || company.filing_available === true;
  container.append(node("p", `company-availability${available ? " available" : " unavailable"}`, demo
    ? "Offline DEMO 10-K available. Not an SEC registry company."
    : company.filing_available === true
      ? "SEC filer · 10-K available for audit."
      : company.filing_available === false
        ? "SEC registry filer · No 10-K available for audit. Other filing forms do not qualify."
        : "10-K availability was not saved or could not be confirmed."));
  const latest = company.latest_10k;
  if (!demo && latest) {
    container.append(node("p", "company-filing-detail", `Latest 10-K · Accession ${latest.accession || "unavailable"} · Report date ${latest.report_date || "unavailable"} · Filed ${latest.filing_date || "unavailable"}`));
    appendSECLink(container, latest.url, "View latest SEC 10-K ↗", company);
  } else if (!demo && available) {
    container.append(node("p", "company-detail-note", "Latest 10-K metadata is unavailable in this snapshot."));
  }
  if (company.offline === true && !demo) container.append(node("p", "company-detail-note", "Server reports offline / cached company details."));
  appendSECLink(container, company.source_url, "View SEC company source ↗", company);
  if (saved && !Object.hasOwn(company, "filing_available")) container.append(node("p", "company-detail-note", "This older audit has no saved registry / filing-availability details. Select the company for a fresh lookup before starting another audit."));
}

function ensureCompanyOption(company) {
  const cik = companyCIK(company.cik);
  const demo = demoCompany(company);
  if (!demo && !cik) return null;
  let option = [...$("ticker").options].find((item) => demo ? item.value === "DEMO" : item.dataset.cik === cik);
  if (!option && company.ticker && !demo) {
    option = [...$("ticker").options].find((item) => item.dataset.preset === "true" && !item.dataset.cik && item.value === company.ticker);
  }
  if (!option) {
    option = node("option");
    option.value = demo ? "DEMO" : cik;
    $("ticker").append(option);
  }
  if (cik) option.dataset.cik = cik;
  option.textContent = `${company.ticker || `CIK ${cik}`} — ${company.name || "SEC filer"}`;
  return option;
}

function canStartAudit() {
  const company = companyPicker.selected;
  const option = $("ticker").selectedOptions[0];
  const matchesSelection = company && option && (demoCompany(company) ? option.value === "DEMO" : option.dataset.cik === companyCIK(company.cik));
  return companyPicker.ready && !companyPicker.loading && !companyPicker.running && Boolean(matchesSelection && (demoCompany(company) || company.filing_available === true));
}

function updateStartButton() {
  $("start-button").disabled = !canStartAudit();
}

async function selectCompany(identifier) {
  const sequence = ++companyPicker.detailSequence;
  const cik = identifier === "DEMO" ? "DEMO" : companyCIK(identifier);
  companyPicker.selected = null;
  companyPicker.loading = Boolean(cik);
  $("selected-company-panel").setAttribute("aria-busy", String(companyPicker.loading));
  $("selected-company-details").replaceChildren();
  $("selected-company-status").classList.remove("error");
  $("selected-company-status").textContent = cik ? `Loading ${cik === "DEMO" ? "offline DEMO" : `SEC CIK ${cik}`} details and 10-K availability…` : "This company has no valid CIK. Search its name or enter its SEC CIK.";
  updateStartButton();
  if (!cik) {
    $("selected-company-status").classList.add("error");
    return;
  }
  try {
    const company = await api(`/api/companies/${encodeURIComponent(cik)}`);
    if (sequence !== companyPicker.detailSequence) return;
    if (!company || typeof company !== "object" || (cik === "DEMO" ? !demoCompany(company) : companyCIK(company.cik) !== cik || demoCompany(company))) throw new Error("The registry response did not identify the requested company.");
    const option = ensureCompanyOption(company);
    $("ticker").value = option.value;
    companyPicker.selected = company;
    $("selected-company-status").textContent = "";
    renderCompanyDetails($("selected-company-details"), company);
  } catch (error) {
    if (sequence !== companyPicker.detailSequence) return;
    $("selected-company-status").classList.add("error");
    if (cik === "DEMO") {
      companyPicker.selected = { ticker: "DEMO", cik: "DEMO", name: "Offline demo filing", filing_available: true, latest_10k: null, offline: true };
      $("ticker").value = "DEMO";
      renderCompanyDetails($("selected-company-details"), companyPicker.selected);
      $("selected-company-status").textContent = "Company lookup unavailable. The existing offline DEMO remains usable.";
    } else {
      $("selected-company-status").textContent = `Company details unavailable for CIK ${cik}: ${error.message}. No audit was created.`;
    }
    const retry = node("button", "button button-secondary company-retry", "Retry company lookup");
    retry.type = "button";
    retry.addEventListener("click", () => selectCompany(cik));
    $("selected-company-details").append(retry);
  } finally {
    if (sequence === companyPicker.detailSequence) {
      companyPicker.loading = false;
      $("selected-company-panel").setAttribute("aria-busy", "false");
      updateStartButton();
    }
  }
}

function selectPickerCompany() {
  const option = $("ticker").selectedOptions[0];
  return selectCompany(option?.value === "DEMO" ? "DEMO" : option?.dataset.cik || companyCIK(option?.value));
}

function addCIKLookup(query) {
  const cik = companyCIK(query);
  if (!cik) return;
  const button = node("button", "company-result company-cik-lookup", `Look up CIK ${cik} directly (10-K availability not yet checked)`);
  button.type = "button";
  button.addEventListener("click", () => selectCompany(cik));
  $("company-search-results").append(button);
}

$("company-query").addEventListener("input", () => {
  companyPicker.searchSequence += 1;
  $("company-search-form").setAttribute("aria-busy", "false");
  $("company-search-button").disabled = !companyPicker.ready;
  $("company-search-status").classList.remove("error");
  $("company-search-status").textContent = "";
  $("company-search-results").replaceChildren();
  if (companyPicker.ready) addCIKLookup($("company-query").value.trim());
});

$("company-search-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!companyPicker.ready) return;
  const query = $("company-query").value.trim();
  const sequence = ++companyPicker.searchSequence;
  $("company-search-results").replaceChildren();
  $("company-search-status").classList.remove("error");
  if (!query || /(?:https?:\/\/|www\.)/i.test(query)) {
    $("company-search-status").classList.add("error");
    $("company-search-status").textContent = "Enter a company name, ticker or CIK, not a URL.";
    return;
  }
  $("company-search-button").disabled = true;
  $("company-search-form").setAttribute("aria-busy", "true");
  $("company-search-status").textContent = "Searching the SEC registry…";
  addCIKLookup(query);
  try {
    const companies = await api(`/api/companies/search?q=${encodeURIComponent(query)}`);
    if (sequence !== companyPicker.searchSequence) return;
    if (!Array.isArray(companies)) throw new Error("Unexpected registry search response.");
    const seen = new Set();
    const results = companies.slice(0, 20).filter((company) => {
      const cik = companyCIK(company?.cik);
      if (!cik || seen.has(cik)) return false;
      seen.add(cik);
      return true;
    });
    $("company-search-status").textContent = results.length
      ? `${results.length} SEC registry match${results.length === 1 ? "" : "es"}${companies.length >= 20 ? " (first 20; refine your search)" : ""}. Select a filer to check 10-K availability.`
      : "No SEC registry matches. Try another name or ticker, or enter a CIK directly. Private / non-SEC companies are not supported.";
    for (const company of results) {
      const button = node("button", "company-result");
      button.type = "button";
      button.append(node("strong", "", company.name || "SEC filer"), node("span", "", `${company.ticker || "No listed ticker"} · CIK ${companyCIK(company.cik)}${company.exchange ? ` · ${company.exchange}` : ""}`), node("small", "", "SEC registry result · 10-K availability not yet checked"));
      button.addEventListener("click", () => selectCompany(company.cik));
      $("company-search-results").append(button);
    }
  } catch (error) {
    if (sequence !== companyPicker.searchSequence) return;
    $("company-search-status").classList.add("error");
    $("company-search-status").textContent = `SEC registry search unavailable: ${error.message}. Try again, use a preset, or look up a CIK directly.`;
  } finally {
    if (sequence === companyPicker.searchSequence) {
      $("company-search-button").disabled = !companyPicker.ready;
      $("company-search-form").setAttribute("aria-busy", "false");
    }
  }
});

$("ticker").addEventListener("change", selectPickerCompany);

async function loadCompanies() {
  $("ticker").disabled = true;
  try {
    const companies = await api("/api/companies");
    const options = [];
    for (const company of companies) {
      const option = node("option", "", `${company.ticker} — ${company.name}`);
      option.value = company.ticker;
      option.dataset.preset = "true";
      const cik = companyCIK(company.cik);
      if (cik) option.dataset.cik = cik;
      options.push(option);
    }
    if (!options.some((option) => option.value === "DEMO")) {
      const demo = node("option", "", "DEMO — Offline demo filing");
      demo.value = "DEMO";
      options.unshift(demo);
    }
    $("ticker").replaceChildren(...options);
  } catch (error) {
    toast(`Company presets unavailable: ${error.message}. Offline DEMO remains available.`);
  } finally {
    $("ticker").disabled = false;
  }
  companyPicker.ready = true;
  $("company-search-button").disabled = false;
  await selectPickerCompany();
}

async function loadRuns(selectLatest = false) {
  const runs = await api("/api/runs");
  $("run-count").textContent = runs.length;
  $("run-list").replaceChildren();
  if (!runs.length) $("run-list").append(node("p", "empty-note", "No audits yet. Start with the offline demo or choose a live SEC filing."));
  for (const run of runs) {
    const button = node("button", `run-item${state.run?.id === run.id ? " active" : ""}${run.pending_reviews ? " review-required" : ""}`);
    button.type = "button";
    const top = node("div", "run-item-top");
    top.append(node("strong", "", `${run.ticker} · ${run.company_name}`), node("span", `run-dot ${run.status}`));
    button.append(top, node("small", "", `${statusLabel(run.status)} · ${formatDate(run.created_at)}`));
    if (run.pending_reviews) button.append(node("small", "", `${run.pending_reviews} pending review${run.pending_reviews === 1 ? "" : "s"}`));
    button.addEventListener("click", () => selectRun(run.id));
    $("run-list").append(button);
  }
  if (selectLatest && runs.length) await selectRun(runs[0].id, false);
}

async function selectRun(runId, refreshList = true) {
  try {
    const previousRunId = state.run?.id;
    state.run = await api(`/api/runs/${runId}`);
    if (previousRunId !== state.run.id) resetAssistant();
    renderRun();
    if (refreshList) await loadRuns();
  } catch (error) { toast(error.message); }
}

function renderRun() {
  const run = state.run;
  if (!run) return;
  if (narration.utterance && (narration.runId !== run.id || narration.snapshot !== JSON.stringify(run.state.metrics))) {
    stopNarration("Results changed. Start reading again to hear the current snapshot.");
  }
  $("welcome").hidden = true;
  $("run-content").hidden = false;
  const { company, filing, metrics, report } = run.state;
  renderCompanyDetails($("audit-company-details"), company, true);
  const companyOption = ensureCompanyOption(company);
  if (companyOption) {
    const useCompany = node("button", "button button-secondary", "Use this company for a new audit");
    useCompany.type = "button";
    useCompany.addEventListener("click", () => {
      $("ticker").value = companyOption.value;
      selectPickerCompany();
      $("selected-company-panel").scrollIntoView({ block: "nearest" });
    });
    $("audit-company-details").append(useCompany);
  }
  $("company-name").textContent = company.name;
  $("filing-label").textContent = `${company.ticker || `CIK ${companyCIK(company.cik) || "unavailable"}`} · FORM 10-K`;
  $("assistant-context").textContent = `${company.ticker || companyCIK(company.cik) || company.name} · ${statusLabel(run.status)}`;
  $("filing-meta").replaceChildren();
  if (filing) {
    $("filing-meta").append(document.createTextNode(`Accession ${filing.accession} · Reporting period ${filing.report_date} · `));
    const filingURL = demoCompany(company) ? null : secURL(filing.url);
    if (filingURL) {
      const link = node("a", "", "View original SEC filing ↗");
      link.href = filingURL;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      $("filing-meta").append(link);
    } else {
      $("filing-meta").append(document.createTextNode(demoCompany(company) ? "Offline deterministic filing" : "SEC source link unavailable"));
    }
  } else {
    $("filing-meta").textContent = "Filing retrieval did not complete";
  }
  $("run-status").className = `status-badge ${run.status}`;
  $("run-status").textContent = statusLabel(run.status);
  $("error-banner").hidden = !run.error;
  $("error-banner").textContent = run.error || "";
  $("report-context").textContent = run.status === "completed"
    ? "Current numbers, visual charts, source evidence and review decisions."
    : "Provisional snapshot with charts and evidence. Pending values are clearly labeled.";

  const pending = run.reviews.filter((review) => review.status === "pending").length;
  const automatic = metrics.filter((metric) => metric.status === "auto_approved").length;
  $("summary-metrics").textContent = metrics.length;
  $("summary-auto").textContent = automatic;
  $("summary-pending").textContent = pending;
  const pendingCard = $("summary-pending").closest(".summary-card");
  pendingCard.classList.toggle("review-required", pending > 0);
  pendingCard.querySelector("small").textContent = pending ? "Human decision required" : run.reviews.length ? "All human reviews completed" : "No human review required";
  $("run-status").classList.toggle("review-required", pending > 0);
  $("summary-accuracy").textContent = report?.accuracy_percentage === null || !report ? "Pending" : `${report.accuracy_percentage}%`;
  $("summary-comparisons").textContent = report ? `${report.ground_truth_matches} of ${report.ground_truth_compared} values matched` : "Final ground-truth check";
  renderWorkflow(run);
  renderAgent(run.state.agent);
  renderMetrics(metrics);
  renderReviews(run.reviews);
  renderAudit(run.audit_log, run.audit_verification);
  renderVisualizations(run);
  renderOrchestration();
  renderSpeechControls();
}

function renderWorkflow(run) {
  const steps = [...document.querySelectorAll("[data-step]")];
  steps.forEach((step) => { step.className = ""; });
  const stageIndex = { created: 0, extracted: 1, validated: 2, review_decisions: 3, awaiting_human_review: 3, final_verification: 4, complete: 4 }[run.stage] ?? 0;
  steps.forEach((step, index) => {
    if (run.status === "completed" || index < stageIndex) step.className = "done";
    else if (index === stageIndex) step.className = run.status === "failed" ? "active" : (run.status === "awaiting_review" ? "active" : "done");
    step.classList.toggle("review-required", step.dataset.step === "review" && run.reviews.some((review) => review.status === "pending"));
  });
}

function renderAgent(agent) {
  $("agent-panel").hidden = !agent;
  $("agent-framework").hidden = !agent;
  if (!agent) return;
  $("agent-framework").textContent = agent.mcp_transport ? "LangGraph + MCP" : "LangGraph agent";
  $("agent-checkpoint").textContent = `${agent.checkpoint_store} checkpoint · thread ${agent.thread_id.slice(0, 8)} · ${agent.available_tools.length} tools${agent.mcp_transport ? " · MCP stdio" : ""}`;
  $("agent-trace").replaceChildren();
  for (const event of agent.tool_trace) {
    const item = node("div", `agent-event ${event.selected_tool === "human_review" ? "agent-interrupt" : ""}`);
    const top = node("div", "agent-event-top");
    top.append(node("span", "agent-sequence", String(event.sequence).padStart(2, "0")), node("strong", "", event.selected_tool.replaceAll("_", " ")));
    item.append(top, node("p", "", event.reason));
    $("agent-trace").append(item);
  }
}

function validationSummary(metric) {
  const container = node("div", "validation-icons");
  for (const check of metric.validations || []) {
    const needsReview = metric.status === "pending" && check.status === "failed";
    const icon = node("span", `validation-icon ${check.status}${needsReview ? " review-criterion" : ""}`, check.status === "passed" ? "✓" : check.status === "failed" ? "!" : "–");
    icon.title = `${check.rule}: ${check.message}${needsReview ? " · Human review required" : ""}`;
    container.append(icon);
  }
  return container;
}

function renderMetrics(metrics) {
  $("metrics-body").replaceChildren();
  for (const metric of metrics) {
    const pending = metric.status === "pending";
    const lowConfidence = metric.confidence < state.run.state.confidence_threshold;
    const row = node("tr", `metric-row${pending ? " review-required" : ""}`);
    row.dataset.metricKey = metric.key;
    row.tabIndex = 0;
    const name = node("td");
    name.append(node("span", "metric-name", metric.label), node("span", "metric-period", `${metric.period} · ${metric.unit}`));
    if (metric.explanation) {
      name.append(node("span", "metric-period", metric.explanation.generated_by === "llm" ? "AI reason available ↗" : "Rule-based reason available ↗"));
    }
    const value = node("td", `metric-value${pending && metric.value === null ? " review-criterion" : ""}`, formatValue(metric.value, metric.unit));
    const confidenceCell = node("td");
    const confidence = node("div", `confidence${lowConfidence ? " low" : ""}${pending && lowConfidence ? " review-criterion" : ""}`);
    const track = node("div", "confidence-track");
    const fill = node("div", "confidence-fill");
    fill.style.width = `${Math.round(metric.confidence * 100)}%`;
    track.append(fill);
    confidence.append(track, node("span", "", `${Math.round(metric.confidence * 100)}%`));
    confidenceCell.append(confidence);
    const validation = node("td");
    validation.append(validationSummary(metric));
    const status = node("td", `row-status ${metric.status}`, pending ? "Human review required" : statusLabel(metric.status));
    if (metric.verification && metric.verification.status !== "unavailable") {
      status.append(node("span", "metric-period", metric.verification.matches ? "XBRL matched" : "XBRL mismatch"));
    }
    row.append(name, value, confidenceCell, validation, status);
    const open = () => openMetric(metric.key);
    row.addEventListener("click", open);
    row.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") open(); });
    $("metrics-body").append(row);
  }
}

function renderReviews(reviews) {
  const pending = reviews.filter((review) => review.status === "pending");
  $("review-count").textContent = pending.length;
  $("review-count").classList.toggle("review-required", pending.length > 0);
  $("review-list").replaceChildren();
  if (!reviews.length) {
    $("review-list").append(node("div", "review-empty", "No values required escalation. All eligible metrics were automatically approved."));
    return;
  }
  const sorted = [...reviews].sort((a, b) => (a.status === "pending" ? -1 : 1) - (b.status === "pending" ? -1 : 1));
  for (const review of sorted) {
    const button = node("button", `review-card ${review.status}${review.status === "pending" ? " review-required" : ""}`);
    button.type = "button";
    const top = node("div", "review-card-top");
    top.append(node("strong", "", review.label), node("span", `row-status ${review.status}`, statusLabel(review.status)));
    button.append(top, node("p", "", review.reason));
    button.append(node("small", "", review.status === "pending" ? `${formatValue(review.original_value)} · ${Math.round(review.confidence * 100)}% confidence — Review now` : `Decided by ${review.reviewer}`));
    button.addEventListener("click", () => openMetric(review.metric_key));
    $("review-list").append(button);
  }
}

function renderAudit(entries, verification) {
  $("audit-body").replaceChildren();
  const integrity = $("audit-integrity");
  integrity.className = `integrity-badge ${entries.length ? (verification.valid ? "valid" : "invalid") : ""}`;
  integrity.textContent = entries.length ? `${verification.valid ? "✓ Hash chain verified" : "⚠ Integrity check failed"} · ${entries.length} decisions` : "No decisions yet";
  if (!entries.length) {
    const row = node("tr");
    const cell = node("td", "empty-cell", "Human decisions will appear here with a tamper-evident hash chain.");
    cell.colSpan = 7;
    row.append(cell);
    $("audit-body").append(row);
    return;
  }
  for (const entry of [...entries].reverse()) {
    const row = node("tr");
    row.append(
      node("td", "", formatDate(entry.timestamp)),
      node("td", "metric-name", entry.metric_key.replaceAll("_", " ")),
      node("td", `row-status ${entry.decision}`, entry.decision),
      node("td", "", formatValue(entry.original_value)),
      node("td", "", formatValue(entry.corrected_value)),
      node("td", "", entry.reviewer),
      node("td", "hash", `${entry.entry_hash.slice(0, 10)}…`),
    );
    row.title = entry.note || "No reviewer note";
    $("audit-body").append(row);
  }
}

function openMetric(metricKey) {
  const metric = state.run.state.metrics.find((item) => item.key === metricKey);
  if (!metric) return;
  if (narration.scope === "metric") stopNarration();
  state.selectedMetric = metric;
  $("dialog-eyebrow").textContent = `${companyLabel(state.run.state.company)} · ${metric.period}`;
  $("dialog-title").textContent = metric.label;
  const content = $("dialog-content");
  content.replaceChildren();
  content.append(node("div", "detail-value", formatValue(metric.value, metric.unit)));
  content.append(node("div", `detail-meta${metric.status === "pending" ? " review-required" : ""}`, `${Math.round(metric.confidence * 100)}% confidence · ${metric.status === "pending" ? "Human review required" : statusLabel(metric.status)} · ${metric.unit}`));
  if (metric.explanation) {
    const explanation = metric.explanation;
    const isAI = explanation.generated_by === "llm";
    const box = node("section", `explanation-box ${isAI ? "ai" : "fallback"}`);
    const heading = node("div", "explanation-heading");
    heading.append(
      node("h3", "", "Metric explanation"),
      node("span", `explanation-badge ${isAI ? "ai" : "fallback"}`, isAI ? "AI-generated" : "Rule-based fallback"),
    );
    box.append(heading, speechControls("metric"), node("p", "explanation-summary", explanation.summary));
    if (explanation.extraction_reason) {
      box.append(node("h4", "", "Why this value was extracted"), node("p", "explanation-summary", explanation.extraction_reason));
    }
    if (explanation.possible_causes?.length) {
      box.append(node("h4", "", "Possible causes — not confirmed facts"));
      const causes = node("ul", "explanation-causes");
      for (const cause of explanation.possible_causes) causes.append(node("li", "", cause));
      box.append(causes);
    }
    const guidance = node("div", "explanation-guidance");
    guidance.append(node("strong", "", "Reviewer guidance"), node("p", "", explanation.review_guidance));
    box.append(guidance);
    const attribution = isAI
      ? `${explanation.provider} · ${explanation.model}`
      : explanation.llm_error
        ? "AI provider unavailable · deterministic rules used instead"
        : "Deterministic rules · no LLM-generated content";
    box.append(node("small", "explanation-attribution", attribution));
    box.append(node("small", "explanation-attribution", "Based on initial extraction; not regenerated after review decisions."));
    content.append(box);
  }
  const evidence = node("section", "evidence-box");
  evidence.append(node("h3", "", "Original source evidence"), node("blockquote", "", metric.source_snippet));
  evidence.append(node("code", "", `${metric.source_name} · Context: ${metric.context_ref || "unavailable"}`));
  const sourceURL = demoCompany(state.run.state.company) ? null : secURL(metric.source_url);
  if (sourceURL) {
    const link = node("a", "", "  Open filing ↗");
    link.href = sourceURL;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    evidence.append(link);
  }
  content.append(evidence);
  const validations = node("section", "validation-detail");
  validations.append(node("h3", "", "Validation results"));
  for (const check of metric.validations || []) {
    const needsReview = metric.status === "pending" && check.status === "failed";
    const item = node("div", `validation-item${needsReview ? " review-required" : ""}`);
    item.append(node("span", `validation-icon ${check.status}${needsReview ? " review-criterion" : ""}`, check.status === "passed" ? "✓" : check.status === "failed" ? "!" : "–"));
    const text = node("div");
    text.append(node("strong", "", check.rule.replaceAll("_", " ")), node("p", "", check.message));
    item.append(text);
    validations.append(item);
  }
  content.append(validations);
  if (metric.ground_truth) {
    content.append(node("div", "truth-box", `XBRL Company Facts ground truth: ${formatValue(metric.ground_truth.value, metric.unit)} · ${metric.ground_truth.tag} · Filed ${metric.ground_truth.filed}`));
  } else {
    content.append(node("div", "truth-box", "No matching XBRL Company Facts value was available for this reporting context."));
  }
  const review = state.run.reviews.find((item) => item.metric_key === metricKey && item.status === "pending");
  $("review-form").hidden = !review;
  $("review-form").classList.toggle("review-required", Boolean(review));
  $("review-form").dataset.reviewId = review?.id || "";
  $("corrected-value").value = review && metric.value !== null ? metric.value : "";
  $("review-note").value = "";
  renderSpeechControls();
  $("metric-dialog").showModal();
}

$("run-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("start-button");
  if (!canStartAudit()) return;
  stopNarration();
  companyPicker.running = true;
  updateStartButton();
  button.textContent = "Retrieving & validating…";
  orchestration.starting = $("ticker").value;
  stopFlowReplay();
  renderOrchestration();
  try {
    const run = await api("/api/runs", {
      method: "POST",
      body: JSON.stringify({
        ticker: $("ticker").value,
        confidence_threshold: Number($("confidence").value),
        variance_threshold: Number($("variance").value),
      }),
    });
    const previousRunId = state.run?.id;
    state.run = run;
    if (previousRunId !== run.id) resetAssistant();
    renderRun();
    await loadRuns();
    toast(run.status === "failed" ? `Audit failed: ${run.error}` : run.status === "awaiting_review" ? "Workflow paused for human review." : "Audit completed without escalations.");
  } catch (error) { toast(error.message); }
  finally {
    companyPicker.running = false;
    updateStartButton();
    button.textContent = "Start audit";
    orchestration.starting = null;
    renderOrchestration();
  }
});

$("refresh-button").addEventListener("click", async () => {
  try {
    await loadRuns();
    if (state.run) await selectRun(state.run.id, false);
  } catch (error) { toast(error.message); }
});

$("report-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const run = state.run;
  if (!run || $("report-button").disabled) return;
  const format = $("report-format").value;
  const formatLabel = format === "pdf" ? "PDF" : "Word";
  $("report-button").disabled = true;
  $("report-format").disabled = true;
  $("report-button-label").textContent = "Generating report…";
  $("report-form").setAttribute("aria-busy", "true");
  try {
    const response = await fetch(`/api/runs/${encodeURIComponent(run.id)}/report?format=${encodeURIComponent(format)}`);
    requireSignIn(response);
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(typeof error.detail === "string" ? error.detail : "Report generation failed. Please try again.");
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = node("a");
    link.href = url;
    link.download = response.headers.get("Content-Disposition")?.match(/filename="([^"]+)"/)?.[1] || `FinFlow-report.${format}`;
    document.body.append(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 60000);
    toast(`${formatLabel} report generated for ${companyLabel(run.state.company)}. Download started.`);
  } catch (error) {
    toast(error.message);
  } finally {
    $("report-button").disabled = false;
    $("report-format").disabled = false;
    $("report-button-label").textContent = "Generate financial report";
    $("report-form").removeAttribute("aria-busy");
  }
});

$("dialog-close").addEventListener("click", () => $("metric-dialog").close());
$("metric-dialog").addEventListener("click", (event) => {
  if (event.target === $("metric-dialog")) $("metric-dialog").close();
});
$("metric-dialog").addEventListener("close", () => {
  if ($("metric-dialog").open) return;
  if (narration.scope === "metric") stopNarration();
  state.selectedMetric = null;
  $("dialog-content").replaceChildren();
  $("review-form").hidden = true;
  $("review-form").classList.remove("review-required");
  renderSpeechControls();
});

$("assistant-toggle").addEventListener("click", () => setAssistantOpen(!state.assistantOpen));
$("assistant-close").addEventListener("click", () => setAssistantOpen(false));
$("chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = $("chat-input");
  const question = input.value.trim();
  if (!question) return;
  const history = state.chat.slice(-8);
  appendChatMessage("user", question);
  input.value = "";
  input.disabled = true;
  $("chat-send").disabled = true;
  const loading = appendChatMessage("assistant", "Reviewing the current dashboard…", null, false);
  loading.classList.add("loading");
  try {
    const result = await api("/api/assistant", {
      method: "POST",
      body: JSON.stringify({ message: question, run_id: state.run?.id || null, history }),
    });
    loading.remove();
    appendChatMessage("assistant", result.answer, chatAttribution(result));
  } catch (error) {
    loading.remove();
    appendChatMessage("assistant", `I couldn’t answer that right now: ${error.message}`, null, false);
  } finally {
    input.disabled = false;
    $("chat-send").disabled = false;
    input.focus();
  }
});

document.querySelectorAll("[data-decision]").forEach((button) => {
  button.addEventListener("click", () => { state.decision = button.dataset.decision; });
});

$("review-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const decision = state.decision;
  const corrected = $("corrected-value").value;
  if (!decision) return;
  if (decision === "correct" && corrected === "") {
    toast("Enter a corrected numeric value first.");
    return;
  }
  const reviewId = event.target.dataset.reviewId;
  const buttons = event.target.querySelectorAll("button");
  buttons.forEach((button) => { button.disabled = true; });
  try {
    state.run = await api(`/api/runs/${state.run.id}/reviews/${reviewId}`, {
      method: "POST",
      body: JSON.stringify({
        decision,
        corrected_value: decision === "correct" ? Number(corrected) : null,
        reviewer: $("reviewer").value,
        note: $("review-note").value,
      }),
    });
    $("metric-dialog").close();
    renderRun();
    await loadRuns();
    toast(state.run.status === "completed" ? "Decision saved. Workflow resumed and XBRL verification completed." : "Decision saved. The next pending review remains queued.");
  } catch (error) { toast(error.message); }
  finally { buttons.forEach((button) => { button.disabled = false; }); }
});

const chartColors = { auto_approved: "#58f5d0", approved: "#72beff", pending: "#ff8198", corrected: "#b99aff", rejected: "#ff78bd" };

function chartBar(metric, valueText, width, color, origin = 0) {
  const row = node("button", `chart-row${metric.status === "pending" ? " review-required" : ""}`);
  row.type = "button";
  row.dataset.metricKey = metric.key;
  row.setAttribute("aria-label", `${metric.label}: ${valueText}. Open evidence.`);
  const heading = node("div", "chart-row-heading");
  heading.append(node("span", "", metric.label), node("strong", "", valueText));
  const track = node("div", "chart-track");
  track.style.setProperty("--chart-color", color);
  const fill = node("span", "chart-fill");
  fill.style.width = `${width}%`;
  fill.style.left = `${origin}%`;
  track.append(fill);
  row.append(heading, track);
  row.addEventListener("click", () => openMetric(metric.key));
  return row;
}

function svgNode(tag, attributes = {}, text = null) {
  const element = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [name, value] of Object.entries(attributes)) element.setAttribute(name, String(value));
  if (text !== null) element.textContent = text;
  return element;
}

function renderVisualizations(run) {
  const metrics = run.state.metrics;
  $("viz-empty").hidden = metrics.length > 0;
  $("viz-content").hidden = !metrics.length;
  $("viz-snapshot").textContent = `${companyLabel(run.state.company)} · ${run.status === "completed" ? "Completed snapshot" : "Provisional snapshot"}`;
  if (!metrics.length) {
    $("viz-empty").textContent = "No metrics are available in this audit yet. Missing data is not plotted as zero.";
    return;
  }
  const threshold = run.state.confidence_threshold;
  const average = metrics.reduce((sum, metric) => sum + metric.confidence, 0) / metrics.length;
  const above = metrics.filter((metric) => metric.confidence >= threshold).length;
  const report = run.state.report;
  $("viz-kpis").replaceChildren();
  for (const [label, value, note] of [
    ["Mean confidence", `${(average * 100).toFixed(1)}%`, "Initial extraction scores"],
    ["At threshold", `${above} / ${metrics.length}`, `${(threshold * 100).toFixed(1)}% selected threshold`],
    ["Human decisions", String(run.audit_log.length), "Saved, hash-chained decisions"],
    ["XBRL accuracy", report?.accuracy_percentage == null ? "Not verified" : `${report.accuracy_percentage}%`, report ? `${report.ground_truth_matches} / ${report.ground_truth_compared} compared facts matched` : "Final verification has not run"],
  ]) {
    const card = node("div", "viz-kpi");
    card.append(node("span", "", label), node("strong", "", value), node("small", "", note));
    $("viz-kpis").append(card);
  }
  $("confidence-chart-caption").textContent = `Initial scores on a 0–100% scale. Target: ${(threshold * 100).toFixed(1)}%. Select a bar to inspect its evidence.`;
  $("confidence-chart").style.setProperty("--threshold", `${threshold * 100}%`);
  $("confidence-chart").replaceChildren();
  for (const metric of metrics) {
    const color = metric.status === "pending" ? chartColors.pending : metric.confidence >= threshold ? chartColors.auto_approved : "#f9ba60";
    $("confidence-chart").append(chartBar(metric, `${(metric.confidence * 100).toFixed(1)}%`, metric.confidence * 100, color));
  }
  const categories = Object.entries(chartColors).map(([status, color]) => ({ status, color, count: metrics.filter((metric) => metric.status === status).length }));
  const layout = node("div", "donut-layout");
  const svg = svgNode("svg", { viewBox: "0 0 220 220", class: "neon-donut", role: "img", "aria-label": categories.map((item) => `${statusLabel(item.status)}: ${item.count}`).join("; ") });
  svg.append(svgNode("circle", { cx: 110, cy: 110, r: 82, fill: "none", stroke: "#233253", "stroke-width": 20 }));
  const circumference = 2 * Math.PI * 82;
  let offset = 0;
  const legend = node("div", "donut-legend");
  for (const category of categories) {
    if (!category.count) continue;
    const length = category.count / metrics.length * circumference;
    svg.append(svgNode("circle", { cx: 110, cy: 110, r: 82, fill: "none", stroke: category.color, "stroke-width": 20, "stroke-dasharray": `${length} ${circumference - length}`, "stroke-dashoffset": -offset, transform: "rotate(-90 110 110)" }));
    offset += length;
    const item = node("div");
    const label = node("span", "", statusLabel(category.status));
    label.style.color = category.color;
    item.append(label, node("strong", "", category.count));
    legend.append(item);
  }
  svg.append(svgNode("text", { x: 110, y: 110, "text-anchor": "middle", class: "donut-number" }, metrics.length));
  svg.append(svgNode("text", { x: 110, y: 134, "text-anchor": "middle", class: "donut-label" }, "METRICS"));
  layout.append(svg, legend);
  $("status-chart").replaceChildren(layout);

  const dollarMetrics = metrics.filter((metric) => metric.unit === "USD");
  const values = dollarMetrics.filter((metric) => metric.value !== null).map((metric) => metric.value);
  const minimum = Math.min(0, ...values);
  const maximum = Math.max(0, ...values);
  const range = maximum - minimum || 1;
  const zero = -minimum / range * 100;
  $("value-chart").replaceChildren();
  for (const metric of dollarMetrics) {
    const value = metric.value;
    const row = chartBar(metric, value === null ? "Unavailable" : formatValue(value), value === null ? 0 : Math.abs(value) / range * 100, value !== null && value < 0 ? chartColors.rejected : "#72beff", value !== null && value < 0 ? (value - minimum) / range * 100 : zero);
    const marker = node("span", "chart-zero");
    marker.style.left = `${zero}%`;
    row.querySelector(".chart-track").append(marker);
    $("value-chart").append(row);
  }
  $("eps-chart-note").textContent = metrics.filter((metric) => metric.unit !== "USD").map((metric) => `${metric.label}: ${formatValue(metric.value, metric.unit)} (${metric.unit}, not on the USD scale)`).join(" · ");
  $("validation-chart").replaceChildren();
  $("chart-data-body").replaceChildren();
  for (const metric of metrics) {
    const counts = ["passed", "failed", "skipped"].map((status) => (metric.validations || []).filter((check) => check.status === status).length);
    const total = counts.reduce((sum, count) => sum + count, 0);
    const row = node("div", `validation-row${metric.status === "pending" ? " review-required" : ""}`);
    const heading = node("div", "chart-row-heading");
    heading.append(node("span", "", metric.label), node("strong", "", total ? counts.join(" / ") : "No checks"));
    const stack = node("div", "validation-stack");
    stack.setAttribute("role", "img");
    stack.setAttribute("aria-label", `${metric.label}: ${counts[0]} passed, ${counts[1]} failed, ${counts[2]} skipped`);
    counts.forEach((count, index) => {
      const segment = node("span");
      segment.style.width = `${total ? count / total * 100 : 0}%`;
      segment.style.background = ["#58f5d0", "#ff78bd", "#7184ac"][index];
      stack.append(segment);
    });
    row.append(heading, stack);
    $("validation-chart").append(row);
    const dataRow = node("tr", metric.status === "pending" ? "review-required" : "");
    dataRow.append(node("th", "", metric.label));
    dataRow.firstChild.scope = "row";
    for (const text of [metric.value === null ? "Unavailable" : `${formatValue(metric.value, metric.unit)} ${metric.unit}`, `${(metric.confidence * 100).toFixed(1)}%`, statusLabel(metric.status), counts.join(" / "), metric.verification ? statusLabel(metric.verification.status) : "Not yet verified"]) dataRow.append(node("td", "", text));
    $("chart-data-body").append(dataRow);
  }
}

const orchestration = { open: false, starting: null, runId: null, replayIndex: null, replayTimer: null, pollTimer: null, paused: window.matchMedia("(prefers-reduced-motion: reduce)").matches, checkedAt: null };
const flowSteps = [
  ["retrieve_filing", "Retrieve filing", "SEC EDGAR / offline DEMO"],
  ["lookup_xbrl_company_facts", "Load XBRL reference", "Independent company facts"],
  ["extract_metrics", "Extract metrics", "Eight evidence-backed facts"],
  ["validate_metrics", "Validate & score", "Rules and confidence threshold"],
  ["explain_metrics", "Explain each metric", "AI or labeled rule-based fallback"],
  ["human_review", "Human review", "Approve, correct or reject"],
  ["verify_ground_truth", "Verify final values", "Compare with XBRL ground truth"],
  ["end", "Complete audit", "Saved results and reports"],
];

function flowTool(event) {
  return event?.selected_tool === "human_decision" ? "human_review" : event?.selected_tool;
}

function stopFlowReplay() {
  window.clearTimeout(orchestration.replayTimer);
  orchestration.replayIndex = null;
}

function setOrchestrationOpen(open) {
  orchestration.open = open;
  $("orchestration-panel").hidden = !open;
  $("orchestration-toggle").setAttribute("aria-expanded", String(open));
  window.clearTimeout(orchestration.pollTimer);
  if (open) {
    setAssistantOpen(false);
    renderOrchestration();
    $("orchestration-close").focus();
    pollOrchestration();
  } else {
    stopFlowReplay();
  }
}

async function pollOrchestration() {
  window.clearTimeout(orchestration.pollTimer);
  if (!orchestration.open) return;
  const selected = state.run;
  if (selected && !orchestration.starting && orchestration.replayIndex === null && !document.hidden) {
    try {
      const updated = await api(`/api/runs/${encodeURIComponent(selected.id)}`);
      if (orchestration.open && state.run === selected && orchestration.replayIndex === null) {
        orchestration.checkedAt = new Date();
        if (JSON.stringify(updated) !== JSON.stringify(selected)) {
          state.run = updated;
          renderRun();
          await loadRuns();
        } else {
          $("flow-sync").textContent = `Live checkpoint checked at ${orchestration.checkedAt.toLocaleTimeString()}`;
        }
      }
    } catch (error) {
      if (orchestration.open && state.run === selected) $("flow-sync").textContent = "Live refresh unavailable. Showing the last saved snapshot; retrying.";
    }
  }
  if (orchestration.open) orchestration.pollTimer = window.setTimeout(pollOrchestration, 3000);
}

function renderOrchestration() {
  const run = state.run;
  if (orchestration.runId !== run?.id) {
    stopFlowReplay();
    orchestration.runId = run?.id;
    orchestration.checkedAt = null;
  }
  const trace = orchestration.starting ? [] : run?.state.agent?.tool_trace || [];
  const replaying = orchestration.replayIndex !== null;
  const event = trace[replaying ? orchestration.replayIndex : trace.length - 1];
  $("orchestration-panel").dataset.replaying = String(replaying);
  $("orchestration-panel").dataset.motion = orchestration.paused ? "paused" : "running";
  $("flow-live").setAttribute("aria-pressed", String(!replaying));
  $("flow-replay").setAttribute("aria-pressed", String(replaying));
  $("flow-replay").disabled = !trace.length;
  $("flow-next").disabled = !replaying || orchestration.replayIndex >= trace.length - 1;
  $("flow-motion").setAttribute("aria-pressed", String(orchestration.paused));
  $("flow-motion").textContent = orchestration.paused ? "Resume animation" : "Pause animation";
  $("orchestration-context").textContent = orchestration.starting ? `${orchestration.starting} · Audit request running` : run ? `${companyLabel(run.state.company)} · ${statusLabel(run.status)}` : "No audit selected";
  const pending = run?.reviews.filter((review) => review.status === "pending").length || 0;
  $("flow-summary").textContent = orchestration.starting ? "Audit running. Waiting for a saved checkpoint; intermediate tool progress is not available." : !run ? "Start an audit to follow its workflow, or choose one from history to replay its recorded steps." : replaying ? `Recorded step ${orchestration.replayIndex + 1} of ${trace.length} · ${statusLabel(event.selected_tool)}` : run.status === "awaiting_review" ? `Workflow paused safely. ${pending} metric${pending === 1 ? "" : "s"} need a human decision before verification.` : run.status === "completed" ? "Audit complete. Final values verified; reports are ready." : run.status === "failed" ? "Audit stopped with an error. No unrecorded steps are marked complete." : "Audit processing. Showing the latest saved checkpoint.";
  $("flow-sync").textContent = replaying ? "Animated replay of saved selections — not live execution" : orchestration.starting ? "The map will update when the audit request returns." : orchestration.checkedAt ? `Live checkpoint checked at ${orchestration.checkedAt.toLocaleTimeString()}` : "Saved-state view · live refresh while this panel is open";
  $("flow-map").replaceChildren();
  const seen = trace.slice(0, replaying ? orchestration.replayIndex : trace.length).map(flowTool);
  const current = flowTool(event);
  for (const [index, [tool, label, description]] of flowSteps.entries()) {
    let status = "queued";
    if (seen.includes(tool)) status = "done";
    if (!orchestration.starting && !replaying && tool === "human_review" && run?.status === "completed" && !seen.includes(tool)) status = "skipped";
    if (tool === current) status = replaying ? "active" : run?.status === "failed" ? "failed" : tool === "human_review" && pending && run?.status === "awaiting_review" ? "waiting" : tool === "end" && run?.status === "completed" ? "done" : run?.status === "processing" ? "active" : "done";
    if (!orchestration.starting && !replaying && tool === "human_review" && pending > 0 && run?.status === "awaiting_review") status = "waiting";
    const reviewRequired = !orchestration.starting && !replaying && tool === "human_review" && status === "waiting" && pending > 0;
    const step = node("button", `flow-step ${status}${reviewRequired ? " review-required" : ""}`);
    step.type = "button";
    step.dataset.tool = tool;
    step.setAttribute("aria-label", `${index + 1}. ${label}: ${status}. Show step details.`);
    if (["active", "waiting", "failed"].includes(status)) step.setAttribute("aria-current", "step");
    const text = node("div");
    text.append(node("strong", "", label), node("small", "", `${status === "skipped" ? "Not required" : statusLabel(status)} · ${description}`));
    step.append(node("span", "flow-node", String(index + 1).padStart(2, "0")), text);
    step.addEventListener("click", () => {
      const saved = trace.slice(0, replaying ? orchestration.replayIndex + 1 : trace.length).findLast((item) => flowTool(item) === tool);
      $("flow-event").replaceChildren(node("strong", "", label), node("p", "", saved?.reason || (status === "skipped" ? "No human decision was required for this audit." : `${description}. This step has no recorded selection in the current view.`)));
    });
    $("flow-map").append(step);
  }
  $("flow-event").replaceChildren(node("strong", "", event ? `Selected tool · ${statusLabel(event.selected_tool)}` : "Waiting for evidence"), node("p", "", event?.reason || "The workflow map shows the real audit sequence. Select any step to see its purpose and recorded selection reason."));
}

function advanceFlowReplay() {
  window.clearTimeout(orchestration.replayTimer);
  const length = state.run?.state.agent?.tool_trace.length || 0;
  if (orchestration.replayIndex === null || orchestration.replayIndex >= length - 1) return;
  orchestration.replayIndex += 1;
  renderOrchestration();
  scheduleFlowReplay();
}

function scheduleFlowReplay() {
  window.clearTimeout(orchestration.replayTimer);
  if (orchestration.open && !orchestration.paused && !document.hidden && orchestration.replayIndex !== null) orchestration.replayTimer = window.setTimeout(advanceFlowReplay, 1200);
}

$("orchestration-toggle").addEventListener("click", () => setOrchestrationOpen(!orchestration.open));
$("orchestration-close").addEventListener("click", () => { setOrchestrationOpen(false); $("orchestration-toggle").focus(); });
$("flow-live").addEventListener("click", () => { stopFlowReplay(); renderOrchestration(); pollOrchestration(); });
$("flow-replay").addEventListener("click", () => { stopFlowReplay(); orchestration.replayIndex = 0; renderOrchestration(); scheduleFlowReplay(); });
$("flow-next").addEventListener("click", advanceFlowReplay);
$("flow-motion").addEventListener("click", () => { orchestration.paused = !orchestration.paused; renderOrchestration(); scheduleFlowReplay(); });
document.addEventListener("visibilitychange", () => { window.clearTimeout(orchestration.replayTimer); if (!document.hidden) { scheduleFlowReplay(); if (orchestration.open) pollOrchestration(); } });
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && orchestration.open && !introOpen() && !$("metric-dialog").open) {
    setOrchestrationOpen(false);
    $("orchestration-toggle").focus();
  }
});
renderOrchestration();

const intro = { index: 0, returnFocus: null, drag: null };
const introSlides = [...document.querySelectorAll(".intro-slide")];

function introOpen() {
  return document.documentElement.classList.contains("show-intro");
}

function showIntroSlide(index) {
  intro.index = Math.max(0, Math.min(index, introSlides.length - 1));
  $("intro-track").style.transform = `translateX(${-100 * intro.index}%)`;
  introSlides.forEach((slide, position) => {
    const active = position === intro.index;
    slide.classList.toggle("active", active);
    slide.inert = !active;
    if (active) slide.scrollTop = 0;
  });
  document.querySelectorAll(".intro-dot").forEach((dot, position) => {
    if (position === intro.index) dot.setAttribute("aria-current", "true");
    else dot.removeAttribute("aria-current");
  });
  const last = intro.index === introSlides.length - 1;
  $("intro-prev").disabled = intro.index === 0;
  $("intro-next").firstChild.textContent = last ? "Open dashboard " : "Next ";
  $("intro-step").textContent = `Slide ${intro.index + 1} of ${introSlides.length}: ${introSlides[intro.index].getAttribute("aria-label").split(": ")[1]}`;
}

function setPageInert(inert) {
  document.querySelectorAll("body > :not(#intro)").forEach((element) => { element.inert = inert; });
}

function openIntro(returnFocus = null) {
  intro.returnFocus = returnFocus;
  document.documentElement.classList.add("show-intro");
  setPageInert(true);
  showIntroSlide(0);
  $("intro-next").focus();
}

function closeIntro() {
  try { sessionStorage.setItem("finflow-intro-seen", "1"); } catch (error) { /* storage unavailable */ }
  document.documentElement.classList.remove("show-intro");
  setPageInert(false);
  (intro.returnFocus || $("workspace")).focus({ preventScroll: true });
}

function nextIntroSlide() {
  if (intro.index === introSlides.length - 1) closeIntro();
  else showIntroSlide(intro.index + 1);
}

$("intro-next").addEventListener("click", nextIntroSlide);
$("intro-prev").addEventListener("click", () => showIntroSlide(intro.index - 1));
$("intro-skip").addEventListener("click", closeIntro);
$("intro-open").addEventListener("click", () => openIntro($("intro-open")));
document.querySelectorAll(".intro-dot").forEach((dot) => {
  dot.addEventListener("click", () => showIntroSlide(Number(dot.dataset.slide)));
});
document.addEventListener("keydown", (event) => {
  if (!introOpen()) return;
  if (event.key === "ArrowRight") { event.preventDefault(); nextIntroSlide(); }
  else if (event.key === "ArrowLeft") { event.preventDefault(); showIntroSlide(intro.index - 1); }
  else if (event.key === "Escape") { event.preventDefault(); closeIntro(); }
});

function startIntroDrag(id, x, y) {
  intro.drag = { id, x, y, dx: 0, horizontal: null };
}

function moveIntroDrag(id, x, y) {
  const drag = intro.drag;
  if (!drag || drag.id !== id) return;
  const dx = x - drag.x;
  const dy = y - drag.y;
  if (drag.horizontal === null && Math.hypot(dx, dy) > 8) {
    drag.horizontal = Math.abs(dx) > Math.abs(dy);
    if (!drag.horizontal) { intro.drag = null; return; }
    $("intro-track").classList.add("dragging");
  }
  if (!drag.horizontal) return;
  const atEdge = (intro.index === 0 && dx > 0) || (intro.index === introSlides.length - 1 && dx < 0);
  drag.dx = atEdge ? dx / 3 : dx;
  $("intro-track").style.transform = `translateX(calc(${-100 * intro.index}% + ${drag.dx}px))`;
}

function endIntroDrag(id, cancelled = false) {
  const drag = intro.drag;
  if (!drag || drag.id !== id) return;
  intro.drag = null;
  $("intro-track").classList.remove("dragging");
  if (!drag.horizontal) return;
  const threshold = Math.min(90, $("intro-viewport").clientWidth * 0.18);
  if (!cancelled && drag.dx <= -threshold) nextIntroSlide();
  else if (!cancelled && drag.dx >= threshold) showIntroSlide(intro.index - 1);
  else showIntroSlide(intro.index);
}

// Touch uses touch events because browsers may cancel pointer streams mid-swipe.
const viewport = $("intro-viewport");
viewport.addEventListener("pointerdown", (event) => {
  if (event.pointerType === "touch" || event.button !== 0) return;
  viewport.setPointerCapture(event.pointerId);
  startIntroDrag(event.pointerId, event.clientX, event.clientY);
});
viewport.addEventListener("pointermove", (event) => moveIntroDrag(event.pointerId, event.clientX, event.clientY));
viewport.addEventListener("pointerup", (event) => endIntroDrag(event.pointerId));
viewport.addEventListener("pointercancel", (event) => endIntroDrag(event.pointerId, true));
viewport.addEventListener("touchstart", (event) => {
  if (event.touches.length !== 1) { intro.drag = null; showIntroSlide(intro.index); return; }
  const touch = event.touches[0];
  startIntroDrag("touch", touch.clientX, touch.clientY);
}, { passive: true });
viewport.addEventListener("touchmove", (event) => {
  const touch = event.touches[0];
  if (touch) moveIntroDrag("touch", touch.clientX, touch.clientY);
}, { passive: true });
viewport.addEventListener("touchend", (event) => {
  const touch = event.changedTouches[0];
  if (touch) moveIntroDrag("touch", touch.clientX, touch.clientY);
  endIntroDrag("touch");
});
viewport.addEventListener("touchcancel", () => endIntroDrag("touch", true));

if (introOpen()) openIntro();
else showIntroSlide(0);

resetAssistant();
$("report-speech-controls").replaceChildren(speechControls("report"));
renderSpeechControls();
window.addEventListener("pagehide", () => stopNarration());

$("logout-button").addEventListener("click", async () => {
  stopNarration();
  $("logout-button").disabled = true;
  try {
    await api("/api/auth/logout", { method: "POST", body: "{}" });
    window.location.replace("/login");
  } catch (error) {
    toast(error.message);
    $("logout-button").disabled = false;
  }
});

window.addEventListener("pageshow", (event) => {
  if (event.persisted) window.location.reload();
});

(async function initialize() {
  try {
    const session = await api("/api/auth/session");
    if (!session.authenticated) {
      window.location.replace("/login");
      return;
    }
    csrfToken = session.csrf_token;
    $("logout-button").disabled = false;
    await loadCompanies();
    await loadRuns(true);
  } catch (error) { toast(`Unable to initialize: ${error.message}`); }
})();
