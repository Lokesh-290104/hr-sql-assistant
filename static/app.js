const chat = document.getElementById("chat");
const form = document.getElementById("composer");
const input = document.getElementById("question");
const sendBtn = document.getElementById("send");
const roleSelect = document.getElementById("role");

// Conversation memory for follow-up questions; the server is stateless.
let history = [];

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

async function ask(question) {
  document.getElementById("welcome")?.remove();
  chat.append(el("div", { class: "msg user" }, question));
  const typing = el("div", { class: "msg bot typing" }, "Writing and checking SQL…");
  chat.append(typing);
  scrollToBottom();
  input.value = "";
  sendBtn.disabled = true;

  try {
    const response = await fetch("/api/query", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, role: roleSelect.value, history }),
    });
    const data = await response.json();
    typing.remove();
    if (!response.ok) {
      const detail = typeof data.detail === "string" ? data.detail : "Request failed.";
      chat.append(renderAnswer({ status: "error", error: detail }));
    } else {
      chat.append(renderAnswer(data));
      // Clarifications stay in history too, so "4 and above" makes sense as the next message.
      history.push({ question, sql: data.status === "ok" ? data.sql : null });
      history = history.slice(-6);
    }
  } catch (err) {
    typing.remove();
    chat.append(renderAnswer({ status: "error", error: "Could not reach the server. Is it running?" }));
  } finally {
    sendBtn.disabled = false;
    input.focus();
    scrollToBottom();
  }
}

async function loadSchema() {
  const container = document.getElementById("schema");
  const tables = await fetch(`/api/schema?role=${roleSelect.value}`).then((r) => r.json());
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
}

async function loadExamples() {
  const examples = await fetch("/api/examples").then((r) => r.json());
  document
    .getElementById("examples")
    .replaceChildren(...examples.map((q) => {
      const chip = el("button", { class: "chip", type: "button" }, q);
      chip.addEventListener("click", () => ask(q));
      return chip;
    }));
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  const question = input.value.trim();
  if (question && !sendBtn.disabled) ask(question);
});

roleSelect.addEventListener("change", () => {
  history = [];
  loadSchema();
});

document.getElementById("newChat").addEventListener("click", () => location.reload());
document.getElementById("toggleSidebar").addEventListener("click", () => {
  document.getElementById("sidebar").classList.toggle("open");
});

loadSchema();
loadExamples();
