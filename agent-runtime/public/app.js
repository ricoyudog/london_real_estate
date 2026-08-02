const API_ROOT = "/v1/sessions";

const COVERAGE_CARDS = [
  { capabilityId: "london-prime-rent", label: "London prime rent" },
  { capabilityId: "london-office-vacancy", label: "London office vacancy" },
  { capabilityId: "uk-investment-transactions", label: "Investment transactions" },
  { capabilityId: "uk-ranked-market-news", label: "Market news" },
];

const elements = {
  artifactContent: document.querySelector("#artifact-content"),
  bankRateCard: document.querySelector("#bank-rate-card"),
  bankRateDetail: document.querySelector("#bank-rate-detail"),
  bankRateMeta: document.querySelector("#bank-rate-meta"),
  bankRateValue: document.querySelector("#bank-rate-value"),
  briefStatus: document.querySelector("#brief-status"),
  chatForm: document.querySelector("#chat-form"),
  chatInput: document.querySelector("#chat-input"),
  connectionStatus: document.querySelector("#connection-status"),
  connectionLabel: document.querySelector("#connection-label"),
  coverageGrid: document.querySelector("#coverage-grid"),
  overviewStatus: document.querySelector("#overview-status"),
  sendButton: document.querySelector("#send-button"),
  sourceDrawer: document.querySelector("#source-drawer"),
  sourceList: document.querySelector("#source-list"),
  sourceSummary: document.querySelector("#source-summary"),
  transcript: document.querySelector("#transcript"),
  turnStatus: document.querySelector("#turn-status"),
};

const state = {
  activeTurnId: null,
  closed: false,
  eventController: null,
  lastEventId: null,
  reconnectTimer: null,
  sessionId: null,
  token: null,
};

document.addEventListener("DOMContentLoaded", () => {
  renderCoverage([]);
  bindInteractions();
  void initialise();
});

function bindInteractions() {
  elements.chatForm.addEventListener("submit", (event) => {
    event.preventDefault();
    void sendMessage();
  });

  for (const button of document.querySelectorAll("[data-prompt]")) {
    button.addEventListener("click", () => {
      if (button instanceof HTMLButtonElement && typeof button.dataset.prompt === "string") {
        elements.chatInput.value = button.dataset.prompt;
        elements.chatInput.focus();
      }
    });
  }

  window.addEventListener("pagehide", () => {
    state.closed = true;
    state.eventController?.abort();
    if (state.reconnectTimer !== null) window.clearTimeout(state.reconnectTimer);
  });
}

async function initialise() {
  setFormAvailability(false);
  setConnection("Starting a secure session…", "loading");

  try {
    const session = await createSession();
    state.sessionId = session.id;
    state.token = session.token;
    setConnection("Secure session active", "ready");
    setFormAvailability(true);
    void fetchOverview();
    void connectEvents();
  } catch {
    setConnection("Unable to start a session", "error");
    elements.overviewStatus.textContent = "The overview is unavailable because a session could not be created.";
    elements.turnStatus.textContent = "The chat is unavailable until a secure session can be started.";
    renderBankRateUnavailable("A canonical Bank Rate record could not be requested.");
    renderCoverage([]);
  }
}

async function createSession() {
  const response = await fetch(API_ROOT, { method: "POST", headers: { accept: "application/json" } });
  const body = await jsonResponse(response);
  const sessionId = textField(body, "session_id") ?? textField(body, "id");
  const token = textField(body, "token") ?? textField(body, "bearer");

  if (!response.ok || sessionId === null || token === null) throw new Error("session creation failed");
  return { id: sessionId, token };
}

async function fetchOverview() {
  if (!hasSession()) return;
  elements.overviewStatus.textContent = "Loading the latest verified record…";

  try {
    const response = await fetch(`${sessionPath()}/dashboard/overview`, { headers: authHeaders() });
    const overview = await jsonResponse(response);
    if (!response.ok || !isRecord(overview) || overview.schema_version !== "dashboard_overview.v1") {
      throw new Error("dashboard overview unavailable");
    }
    renderOverview(overview);
    elements.overviewStatus.textContent = "Canonical snapshot loaded for this session.";
  } catch {
    elements.overviewStatus.textContent = "The canonical snapshot is unavailable right now.";
    renderBankRateUnavailable("No canonical Bank Rate record was returned for this session.");
    renderCoverage([]);
  }
}

function renderOverview(overview) {
  renderBankRate(isRecord(overview.bank_rate) ? overview.bank_rate : null);
  renderCoverage(Array.isArray(overview.coverage) ? overview.coverage : []);
}

function renderBankRate(rate) {
  const value = rate === null ? null : scalar(rate.value);
  const unit = rate === null ? null : scalar(rate.unit);
  const available = rate?.status === "available" && value !== null && unit !== null;

  if (!available) {
    renderBankRateUnavailable(rate === null ? "No canonical Bank Rate record was returned." : scalar(rate.reason) ?? "Canonical Bank Rate coverage is unavailable.");
    return;
  }

  elements.bankRateCard.dataset.status = "available";
  elements.bankRateValue.textContent = measurement(value, unit);
  elements.bankRateDetail.textContent = scalar(rate.definition) ?? "Host-validated Bank Rate record.";
  elements.bankRateMeta.replaceChildren();

  appendMetadata("As of", scalar(rate.as_of));
  appendMetadata("Period", scalar(rate.period_label));
  appendMetadata("Source date", scalar(rate.source_date));
  appendMetadata("Freshness", freshnessText(rate.freshness));
  appendSourceMetadata(rate.source);
}

function renderBankRateUnavailable(detail) {
  elements.bankRateCard.dataset.status = "unavailable";
  elements.bankRateValue.textContent = "Unavailable";
  elements.bankRateDetail.textContent = detail;
  elements.bankRateMeta.replaceChildren();
}

function appendMetadata(label, value) {
  if (value === null) return;
  const group = document.createElement("div");
  const term = document.createElement("dt");
  const description = document.createElement("dd");
  term.textContent = label;
  description.textContent = value;
  group.append(term, description);
  elements.bankRateMeta.append(group);
}

function appendSourceMetadata(source) {
  if (!isRecord(source)) return;
  const label = scalar(source.publisher) ?? scalar(source.title) ?? scalar(source.public_url);
  if (label === null) return;

  const group = document.createElement("div");
  const term = document.createElement("dt");
  const description = document.createElement("dd");
  term.textContent = "Source";
  const sourceUrl = safeHttpUrl(source.public_url);
  if (sourceUrl === null) {
    description.textContent = label;
  } else {
    const link = document.createElement("a");
    link.href = sourceUrl;
    link.target = "_blank";
    link.rel = "noreferrer";
    link.textContent = label;
    description.append(link);
  }
  group.append(term, description);
  elements.bankRateMeta.append(group);
}

function freshnessText(freshness) {
  if (!isRecord(freshness)) return null;
  const values = [
    scalar(freshness.retrieval),
    scalar(freshness.observation),
    typeof freshness.degraded === "boolean" ? freshness.degraded ? "degraded" : "not degraded" : null,
  ].filter((value) => value !== null);
  return values.length === 0 ? null : values.join(" · ");
}

function renderCoverage(rawCoverage) {
  const supplied = Array.isArray(rawCoverage) ? rawCoverage : [];
  const byId = new Map();
  for (const item of supplied) {
    if (!isRecord(item)) continue;
    const id = scalar(item.capability_id);
    if (id === null) continue;
    byId.set(id, {
      id,
      status: coverageStatus(scalar(item.status)),
      detail: scalar(item.reason),
    });
  }

  const coverage = COVERAGE_CARDS.map(({ capabilityId, label }) => {
    const item = byId.get(capabilityId);
    return {
      id: capabilityId,
      label,
      status: item?.status ?? "unavailable",
      detail: item?.detail ?? "No canonical coverage in this launch.",
    };
  });

  elements.coverageGrid.replaceChildren(...coverage.map(coverageCard));
}

function coverageStatus(status) {
  if (status === "supported") return "available";
  if (status === "partial") return "partial";
  return "unavailable";
}

function coverageCard(item) {
  const card = document.createElement("article");
  card.className = "coverage-card";
  card.dataset.status = item.status;

  const top = document.createElement("div");
  top.className = "coverage-card__top";
  const title = document.createElement("h3");
  title.textContent = item.label;
  const status = document.createElement("span");
  status.className = "coverage-state";
  status.dataset.status = item.status;
  status.textContent = humanStatus(item.status);
  top.append(title, status);

  const detail = document.createElement("p");
  detail.textContent = item.detail;
  card.append(top, detail);
  return card;
}

async function sendMessage() {
  const message = elements.chatInput.value.trim();
  if (message === "" || !hasSession() || state.activeTurnId !== null) return;

  appendMessage("user", "Your question", message);
  elements.chatInput.value = "";
  setFormAvailability(false);
  elements.turnStatus.textContent = "Sending your question…";
  setBriefStatus("Working", "waiting");

  try {
    const response = await fetch(`${sessionPath()}/messages`, {
      method: "POST",
      headers: authHeaders({ "content-type": "application/json" }),
      body: JSON.stringify({ message }),
    });
    const result = await jsonResponse(response);
    const turnId = textField(result, "turn_id");
    if (!response.ok || turnId === null) throw new Error("message request failed");
    state.activeTurnId = turnId;
    elements.turnStatus.textContent = "The analyst is preparing a host-validated brief…";
  } catch {
    elements.turnStatus.textContent = "The question could not be sent. Please try again.";
    setBriefStatus("Unavailable", "unavailable");
    setFormAvailability(true);
  }
}

async function connectEvents() {
  if (!hasSession() || state.closed || state.eventController !== null) return;

  const controller = new AbortController();
  state.eventController = controller;
  const headers = authHeaders();
  if (state.lastEventId !== null) headers["last-event-id"] = state.lastEventId;

  try {
    const response = await fetch(`${sessionPath()}/events`, { headers, signal: controller.signal });
    if (!response.ok || response.body === null) {
      const replayEvicted = response.status === 410 && response.headers.get("x-replay-evicted") === "true";
      if (replayEvicted) {
        state.lastEventId = null;
      }
      if (response.status === 401 || (response.status === 410 && !replayEvicted)) expireSession();
      throw new Error("event stream unavailable");
    }
    setConnection("Secure session active", "ready");
    await readSse(response.body);
    if (!controller.signal.aborted) throw new Error("event stream ended");
  } catch {
    if (!controller.signal.aborted && !state.closed && hasSession()) {
      setConnection("Connection interrupted; reconnecting…", "loading");
      scheduleReconnect();
    }
  } finally {
    if (state.eventController === controller) state.eventController = null;
  }
}

function scheduleReconnect() {
  if (state.reconnectTimer !== null || state.closed || !hasSession()) return;
  state.reconnectTimer = window.setTimeout(() => {
    state.reconnectTimer = null;
    void connectEvents();
  }, 1600);
}

function expireSession() {
  state.token = null;
  state.sessionId = null;
  setFormAvailability(false);
  setConnection("This session is no longer active", "error");
  elements.turnStatus.textContent = "Refresh the page to begin another session.";
}

async function readSse(stream) {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buffered = "";

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffered += decoder.decode(value, { stream: true });
      buffered = consumeSseFrames(buffered);
    }
    buffered += decoder.decode();
    consumeSseFrames(buffered);
  } finally {
    reader.releaseLock();
  }
}

function consumeSseFrames(buffer) {
  const frames = buffer.split(/\r?\n\r?\n/);
  const remainder = frames.pop() ?? "";
  for (const frame of frames) handleSseFrame(frame);
  return remainder;
}

function handleSseFrame(frame) {
  let eventName = "message";
  let eventId = null;
  const data = [];

  for (const line of frame.split(/\r?\n/)) {
    if (line.startsWith("event:")) eventName = line.slice(6).trim();
    else if (line.startsWith("id:")) eventId = line.slice(3).trim();
    else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
  }

  if (eventId !== null) state.lastEventId = eventId;
  if (data.length === 0) return;

  try {
    const event = JSON.parse(data.join("\n"));
    if (!isRecord(event)) return;
    if (typeof event.event_id === "string") state.lastEventId = event.event_id;
    handleRuntimeEvent(typeof event.type === "string" ? event.type : eventName, event);
  } catch {
    // A malformed frame is ignored; the next server event remains independently parseable.
  }
}

function handleRuntimeEvent(type, event) {
  const payload = isRecord(event.payload) ? event.payload : {};
  const turnId = scalar(event.turn_id);

  switch (type) {
    case "session.started":
      elements.turnStatus.textContent = "Ready for a source-grounded question.";
      return;
    case "turn.started":
      state.activeTurnId = turnId ?? state.activeTurnId;
      elements.turnStatus.textContent = "The analyst is gathering host-validated evidence…";
      setBriefStatus("Working", "waiting");
      return;
    case "tool.started":
      elements.turnStatus.textContent = "Checking the permitted market data tools…";
      return;
    case "approval.required":
      elements.turnStatus.textContent = "This turn is waiting for a required approval.";
      return;
    case "artifact.final":
      renderArtifact(payload.artifact);
      setBriefStatus(artifactStatus(payload.artifact), artifactStatusKey(payload.artifact));
      appendArtifactMessage(payload.artifact);
      return;
    case "turn.completed":
      finishTurn(turnId, payload.terminal_state === "cancelled" ? "The turn was cancelled." : "The final brief is ready.");
      return;
    case "turn.failed":
      finishTurn(turnId, "The turn ended before a final artifact was available.");
      setBriefStatus("Unavailable", "unavailable");
      return;
    default:
      return;
  }
}

function finishTurn(turnId, status) {
  if (turnId === null || turnId === state.activeTurnId) {
    state.activeTurnId = null;
    setFormAvailability(true);
  }
  elements.turnStatus.textContent = status;
}

function renderArtifact(artifact) {
  elements.artifactContent.replaceChildren();
  elements.sourceList.replaceChildren();
  elements.sourceDrawer.hidden = true;

  if (!isRecord(artifact)) {
    const message = document.createElement("p");
    message.className = "empty-artifact";
    message.textContent = "The runtime completed without a displayable final artifact.";
    elements.artifactContent.append(message);
    return;
  }

  const title = scalar(artifact.title) ?? "Host-validated market brief";
  const heading = document.createElement("h3");
  heading.className = "artifact-title";
  heading.textContent = title;
  elements.artifactContent.append(heading);

  const displayText = scalar(artifact.display_text);
  if (displayText !== null && displayText !== title) {
    const summary = document.createElement("p");
    summary.className = "artifact-summary";
    summary.textContent = displayText;
    elements.artifactContent.append(summary);
  }

  renderFacts(artifact.facts);
  renderInferences(artifact.inferences);
  renderLimitations(artifact.limitations);
  renderSources(artifact.sources);

  if (artifact.schema_version !== "market_brief.v1") renderUnknownArtifact(artifact);
}

function renderFacts(facts) {
  if (!Array.isArray(facts) || facts.length === 0) return;
  const section = artifactSection("Facts");
  const list = document.createElement("ul");
  list.className = "fact-list";

  for (const fact of facts) {
    if (!isRecord(fact)) continue;
    const item = document.createElement("li");
    item.className = "fact-item";
    const label = document.createElement("span");
    label.className = "fact-label";
    label.textContent = scalar(fact.numeric_definition) ?? scalar(fact.claim_id) ?? "Verified fact";
    const value = document.createElement("span");
    value.className = "fact-value";
    value.textContent = fact.kind === "numeric"
      ? measurement(scalar(fact.numeric_value), scalar(fact.numeric_unit))
      : scalar(fact.text) ?? "No displayable fact text.";
    item.append(label, value);

    const asOf = scalar(fact.numeric_as_of);
    if (asOf !== null) {
      const metadata = document.createElement("span");
      metadata.className = "fact-meta";
      metadata.textContent = `As of ${asOf}`;
      item.append(metadata);
    }
    list.append(item);
  }

  if (list.childElementCount > 0) {
    section.append(list);
    elements.artifactContent.append(section);
  }
}

function renderInferences(inferences) {
  if (!Array.isArray(inferences) || inferences.length === 0) return;
  const section = artifactSection("Inferences");
  const list = document.createElement("ol");
  list.className = "inference-list";

  for (const inference of inferences) {
    if (!isRecord(inference)) continue;
    const text = scalar(inference.text);
    if (text === null) continue;
    const item = document.createElement("li");
    item.className = "inference-item";
    const content = document.createElement("span");
    content.className = "inference-text";
    content.textContent = text;
    item.append(content);
    const caveat = scalar(inference.caveat);
    if (caveat !== null) {
      const caveatElement = document.createElement("span");
      caveatElement.className = "inference-caveat";
      caveatElement.textContent = `Caveat: ${caveat}`;
      item.append(caveatElement);
    }
    list.append(item);
  }

  if (list.childElementCount > 0) {
    section.append(list);
    elements.artifactContent.append(section);
  }
}

function renderLimitations(limitations) {
  if (!Array.isArray(limitations) || limitations.length === 0) return;
  const section = artifactSection("Coverage and limitations");
  const list = document.createElement("ul");
  list.className = "limitation-list";
  for (const limitation of limitations) {
    const text = scalar(limitation);
    if (text === null) continue;
    const item = document.createElement("li");
    item.textContent = text;
    list.append(item);
  }
  if (list.childElementCount > 0) {
    section.append(list);
    elements.artifactContent.append(section);
  }
}

function renderSources(sources) {
  if (!Array.isArray(sources) || sources.length === 0) return;
  let sourceCount = 0;
  for (const source of sources) {
    if (!isRecord(source)) continue;
    const name = scalar(source.source);
    if (name === null) continue;
    const item = document.createElement("li");
    item.textContent = name;
    const publishedAt = scalar(source.published_at);
    if (publishedAt !== null) {
      const date = document.createElement("small");
      date.textContent = `Published ${publishedAt}`;
      item.append(date);
    }
    elements.sourceList.append(item);
    sourceCount += 1;
  }
  if (sourceCount > 0) {
    elements.sourceSummary.textContent = `Sources (${sourceCount})`;
    elements.sourceDrawer.hidden = false;
  }
}

function renderUnknownArtifact(artifact) {
  const section = artifactSection("Final artifact");
  const pre = document.createElement("pre");
  pre.className = "artifact-json";
  pre.textContent = JSON.stringify(artifact, null, 2);
  section.append(pre);
  elements.artifactContent.append(section);
}

function artifactSection(title) {
  const section = document.createElement("section");
  section.className = "artifact-section";
  const heading = document.createElement("h3");
  heading.textContent = title;
  section.append(heading);
  return section;
}

function appendMessage(kind, label, content) {
  const message = document.createElement("article");
  message.className = `message message--${kind}`;
  const messageLabel = document.createElement("p");
  messageLabel.className = "message-label";
  messageLabel.textContent = label;
  const body = document.createElement("p");
  body.textContent = content;
  message.append(messageLabel, body);
  elements.transcript.append(message);
  elements.transcript.scrollTop = elements.transcript.scrollHeight;
}

function appendArtifactMessage(artifact) {
  if (!isRecord(artifact)) return;
  const title = scalar(artifact.title) ?? "A final host-validated brief";
  appendMessage("assistant", "Final artifact", title);
}

function setConnection(label, status) {
  elements.connectionLabel.textContent = label;
  elements.connectionStatus.dataset.state = status;
}

function setBriefStatus(label, status) {
  elements.briefStatus.textContent = label;
  elements.briefStatus.dataset.status = status;
}

function setFormAvailability(available) {
  const enabled = available && state.activeTurnId === null && hasSession();
  elements.chatInput.disabled = !enabled;
  elements.sendButton.disabled = !enabled;
}

function authHeaders(extra = {}) {
  return { authorization: `Bearer ${state.token}`, ...extra };
}

function sessionPath() {
  return `${API_ROOT}/${encodeURIComponent(state.sessionId)}`;
}

function hasSession() {
  return typeof state.sessionId === "string" && typeof state.token === "string";
}

async function jsonResponse(response) {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

function isRecord(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function textField(value, key) {
  return isRecord(value) ? scalar(value[key]) : null;
}

function scalar(value) {
  if (typeof value === "string") return value.trim() === "" ? null : value.trim();
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  return null;
}

function measurement(value, unit) {
  if (value === null || unit === null) return "Unavailable";
  return /^[%％]/.test(unit) ? `${value}${unit}` : `${value} ${unit}`;
}

function humanStatus(status) {
  return status.replace(/[_-]+/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function artifactStatus(artifact) {
  return isRecord(artifact) ? humanStatus(scalar(artifact.status) ?? "complete") : "Unavailable";
}

function artifactStatusKey(artifact) {
  return isRecord(artifact) ? scalar(artifact.status) ?? "complete" : "unavailable";
}

function safeHttpUrl(value) {
  const candidate = scalar(value);
  if (candidate === null) return null;
  try {
    const url = new URL(candidate);
    return url.protocol === "https:" || url.protocol === "http:" ? url.href : null;
  } catch {
    return null;
  }
}
