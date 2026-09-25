const chat = document.getElementById("chat");
const form = document.getElementById("composer");
const input = document.getElementById("question");
const sendBtn = document.getElementById("send");
const roleSelect = document.getElementById("role");
const sidebar = document.getElementById("sidebar");
const sidebarToggle = document.getElementById("toggleSidebar");
const welcome = document.getElementById("welcome");

// Matches the server's HISTORY_TURNS: only this many turns reach the LLM anyway.
const MAX_HISTORY = 3;
// Above the server's worst case (LLM deadline x repair attempts), so the server gives up first.
const REQUEST_TIMEOUT_MS = 200_000;

// Conversation memory for follow-up questions; the server is stateless.
let history = [];
// Bumped on "New chat" / role switch so late answers from the old conversation are dropped.
let conversationId = 0;
let inFlight = null;

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") node.className = value;
    else node.setAttribute(key, value);
  }
  for (const child of children) {
    if (child == null) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function scrollToBottom() {
  chat.scrollTop = chat.scrollHeight;
}

function formatCell(value) {
  if (value === null) return "—";
  if (typeof value === "number" && !Number.isInteger(value)) {
    return value.toLocaleString("en-IN", { maximumFractionDigits: 2 });
  }
  if (typeof value === "number") return value.toLocaleString("en-IN");
  return value;
}

function renderTable(columns, rows) {
  const head = el("tr", {}, ...columns.map((c) => el("th", {}, c)));
  const body = rows.map((row) =>
    el("tr", {}, ...row.map((v) => el("td", { class: typeof v === "number" ? "num" : "" }, formatCell(v))))
  );
  return el("div", { class: "table-wrap" }, el("table", {}, el("thead", {}, head), el("tbody", {}, ...body)));
}

function renderAnswer(data) {
  if (data.status === "clarification") {
    return el("div", { class: "msg bot clarify" }, el("p", { class: "explanation" }, data.clarification));
  }
  if (data.status === "error") {
    return el(
      "div",
      { class: "msg bot error" },
      el("p", { class: "explanation" }, data.error || "Something went wrong."),
      data.sql ? el("details", { class: "sql" }, el("summary", {}, "Last attempted SQL"), el("pre", {}, data.sql)) : null
    );
  }

  let result;
  if (data.row_count === 0) {
    result = el("p", { class: "meta" }, "The query ran successfully but returned no rows.");
  } else if (data.row_count === 1 && data.columns.length === 1) {
    result = el("p", {}, el("strong", {}, `${data.columns[0]}: ${formatCell(data.rows[0][0])}`));
  } else {
    result = renderTable(data.columns, data.rows);
  }

  const meta = [
    `${data.row_count} row${data.row_count === 1 ? "" : "s"}${data.truncated ? " (truncated)" : ""}`,
    `${(data.latency_ms / 1000).toFixed(1)}s`,
  ];
  if (data.attempts > 1) meta.push(`self-corrected after ${data.attempts - 1} failed attempt(s)`);

  return el(
    "div",
    { class: "msg bot" },
    el("p", { class: "explanation" }, data.explanation),
    el("details", { class: "sql" }, el("summary", {}, "Show SQL"), el("pre", {}, data.sql)),
    result,
    el("div", { class: "meta" }, meta.join(" · "))
  );
}

// Fetch JSON, turning non-JSON error pages and HTTP errors into readable messages.
async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  let data = null;
  try {
    data = await response.json();
  } catch {
    // Non-JSON body (e.g. a plain-text 500); handled below.
  }
  if (!response.ok) {
    const detail = data && typeof data.detail === "string" ? data.detail : `Request failed (HTTP ${response.status}).`;
    throw new Error(detail);
  }
  if (data === null) throw new Error("The server sent an unexpected response.");
  return data;
}

async function ask(question) {
  if (sendBtn.disabled) return;
  const myConversation = conversationId;
  inFlight = new AbortController();
  welcome.remove();
  chat.append(el("div", { class: "msg user" }, question));
  const typing = el("div", { class: "msg bot typing" }, "Writing and checking SQL…");
  chat.append(typing);
  scrollToBottom();
  input.value = "";
  sendBtn.disabled = true;

  try {
    const data = await fetchJson("/api/query", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, role: roleSelect.value, history }),
      signal: AbortSignal.any([inFlight.signal, AbortSignal.timeout(REQUEST_TIMEOUT_MS)]),
    });
    if (myConversation !== conversationId) return;
    typing.remove();
    chat.append(renderAnswer(data));
    // Clarifications stay in history too, so "4 and above" makes sense as the next message.
    history.push({ question, sql: data.status === "ok" ? data.sql : null });
    history = history.slice(-MAX_HISTORY);
  } catch (err) {
    if (myConversation !== conversationId) return;
    typing.remove();
    let message = err.message;
    if (err.name === "TimeoutError") message = "The request took too long. Please try again.";
    else if (err instanceof TypeError) message = "Could not reach the server. Is it running?";
    chat.append(renderAnswer({ status: "error", error: message }));
  } finally {
    if (myConversation === conversationId) {
      sendBtn.disabled = false;
      input.focus();
      scrollToBottom();
    }
  }
}

async function loadSchema() {
  const container = document.getElementById("schema");
  try {
    const tables = await fetchJson(`/api/schema?role=${encodeURIComponent(roleSelect.value)}`);
    container.replaceChildren(
      ...tables.map((t) =>
        el(
          "details",
          {},
          el("summary", {}, t.name),
          t.description ? el("p", { class: "desc" }, t.description) : null,
          el("ul", {}, ...t.columns.map((c) => el("li", {}, `${c.name} · ${c.type.toLowerCase()}`)))
        )
      )
    );
  } catch (err) {
    container.replaceChildren(el("p", { class: "desc" }, `Could not load tables: ${err.message}`));
  }
}

async function loadExamples() {
  const container = document.getElementById("examples");
  try {
    const examples = await fetchJson("/api/examples");
    container.replaceChildren(
      ...examples.map((q) => {
        const chip = el("button", { class: "chip", type: "button" }, q);
        chip.addEventListener("click", () => ask(q));
        return chip;
      })
    );
  } catch {
    container.replaceChildren();
  }
}

function newConversation(note) {
  conversationId += 1;
  inFlight?.abort();
  sendBtn.disabled = false;
  history = [];
  chat.replaceChildren(welcome);
  if (note) welcome.prepend(el("p", { class: "meta" }, note));
  input.focus();
}

function setSidebar(open) {
  sidebar.classList.toggle("open", open);
  sidebarToggle.setAttribute("aria-expanded", String(open));
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  const question = input.value.trim();
  if (question && !sendBtn.disabled) ask(question);
});

roleSelect.addEventListener("change", () => {
  // Answers from the previous role shouldn't linger on screen or feed follow-ups.
  welcome.querySelector(".meta")?.remove();
  newConversation(`Switched to ${roleSelect.selectedOptions[0].text}. New conversation started.`);
  loadSchema();
});

document.getElementById("newChat").addEventListener("click", () => {
  welcome.querySelector(".meta")?.remove();
  newConversation();
});

sidebarToggle.addEventListener("click", (event) => {
  event.stopPropagation();
  setSidebar(!sidebar.classList.contains("open"));
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") setSidebar(false);
});
document.addEventListener("click", (event) => {
  if (sidebar.classList.contains("open") && !sidebar.contains(event.target)) setSidebar(false);
});

loadSchema();
loadExamples();
