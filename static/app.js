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
// Live charts, re-rendered when the theme changes.
const charts = new Set();

// ---------- DOM helpers ----------

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function scrollToBottom() {
  chat.scrollTop = chat.scrollHeight;
}

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function formatNumber(value) {
  return value.toLocaleString("en-IN", { maximumFractionDigits: Number.isInteger(value) ? 0 : 2 });
}

function formatCell(value) {
  if (value === null) return "—";
  if (typeof value === "number") return formatNumber(value);
  return value;
}

// "avg_salary" -> "Avg salary": SQL aliases are for machines, headers are for people.
function humanize(column) {
  const words = String(column).replace(/[_\s]+/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

// ---------- Theme ----------

function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem("theme", theme); } catch { /* private mode: theme just won't persist */ }
  for (const chart of charts) chart.rerender();
}

document.getElementById("themeToggle").addEventListener("click", () => {
  setTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light");
});

// ---------- SQL display ----------

const SQL_KEYWORDS = new Set(("select from where and or not in is null as on join left right inner outer full cross " +
  "group by order having limit offset union all distinct case when then else end with asc desc between like " +
  "exists interval true false over partition").split(" "));
const CLAUSE_BREAK = /\s+(?=\b(FROM|WHERE|GROUP BY|ORDER BY|HAVING|LIMIT|UNION|(LEFT |RIGHT |INNER |FULL |CROSS )?(OUTER )?JOIN)\b)/g;

// Put major clauses on their own lines (outside string literals only).
function formatSql(sql) {
  return sql
    .split(/('(?:[^']|'')*')/)
    .map((part, i) => (i % 2 ? part : part.replace(CLAUSE_BREAK, "\n")))
    .join("");
}

function highlightSql(sql) {
  const pre = el("pre");
  const token = /('(?:[^']|'')*')|(\b\d+(?:\.\d+)?\b)|([A-Za-z_][A-Za-z0-9_]*)(\s*\()?/g;
  let last = 0;
  for (const m of sql.matchAll(token)) {
    if (m.index > last) pre.append(sql.slice(last, m.index));
    const [text, str, num, word, paren] = m;
    if (str) pre.append(el("span", { class: "sql-str" }, str));
    else if (num) pre.append(el("span", { class: "sql-num" }, num));
    else if (SQL_KEYWORDS.has(word.toLowerCase())) pre.append(el("span", { class: "sql-kw" }, word), paren || "");
    else if (paren) pre.append(el("span", { class: "sql-fn" }, word), paren);
    else pre.append(text);
    last = m.index + text.length;
  }
  pre.append(sql.slice(last));
  return pre;
}

// ---------- CSV ----------

function toCsv(columns, rows) {
  const cell = (v) => {
    const s = v == null ? "" : String(v);
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  return [columns, ...rows].map((r) => r.map(cell).join(",")).join("\n");
}

function downloadCsv(columns, rows, question) {
  const name = (question || "results").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 50);
  const url = URL.createObjectURL(new Blob([toCsv(columns, rows)], { type: "text/csv" }));
  const a = el("a", { href: url, download: `${name || "results"}.csv` });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// ---------- Charts ----------

const TIME_NAME = /(date|day|week|month|quarter|year|period)/i;
const TIME_VALUE = /^\d{4}(-\d{2}(-\d{2})?|-[HQ]\d)?$/;

// Decide whether (and how) a result can be charted: one label dimension + 1-3 numeric measures.
function chartSpec(columns, rows) {
  if (rows.length < 2 || rows.length > 60) return null;
  const isNumeric = columns.map((_, i) => rows.every((r) => r[i] === null || typeof r[i] === "number") && rows.some((r) => r[i] !== null));
  let labelIdx = columns.map((_, i) => i).filter((i) => !isNumeric[i]);
  let valueIdx = columns.map((_, i) => i).filter((i) => isNumeric[i]);

  // A numeric year/period column (e.g. YEAR(hire_date)) is a label, not a measure.
  if (!labelIdx.length && valueIdx.length >= 2 && TIME_NAME.test(columns[valueIdx[0]])) {
    labelIdx = [valueIdx[0]];
    valueIdx = valueIdx.slice(1);
  }
  if (labelIdx.length < 1 || labelIdx.length > 2 || valueIdx.length < 1 || valueIdx.length > 3) return null;

  const labels = rows.map((r) => labelIdx.map((i) => (r[i] == null ? "—" : String(r[i]))).join(" "));
  const isTime = labelIdx.length === 1 && (TIME_NAME.test(columns[labelIdx[0]]) || labels.every((l) => TIME_VALUE.test(l)));
  const longLabels = labels.some((l) => l.length > 14);
  return {
    type: isTime ? "line" : "bar",
    horizontal: !isTime && (rows.length > 8 || longLabels),
    labels,
    series: valueIdx.map((i) => ({ name: humanize(columns[i]), data: rows.map((r) => r[i]) })),
  };
}

function renderChart(spec) {
  const box = el("div", { class: "chart-box" + (spec.horizontal && spec.labels.length > 10 ? " tall" : "") });
  const canvas = el("canvas", { role: "img", "aria-label": `Chart of ${spec.series.map((s) => s.name).join(", ")}` });
  box.append(canvas);
  let chart = null;

  const draw = () => {
    chart?.destroy();
    Chart.defaults.font.family = cssVar("--font-sans");
    const palette = [cssVar("--accent"), "#f59e0b", "#60a5fa"];
    const grid = cssVar("--border");
    const text = cssVar("--muted");
    chart = new Chart(canvas, {
      type: spec.type,
      data: {
        labels: spec.labels,
        datasets: spec.series.map((s, i) => ({
          label: s.name,
          data: s.data,
          backgroundColor: spec.type === "line" ? palette[i] + "22" : palette[i],
          borderColor: palette[i],
          borderWidth: spec.type === "line" ? 2 : 0,
          borderRadius: 4,
          maxBarThickness: 36,
          fill: spec.type === "line" && spec.series.length === 1,
          tension: 0.3,
          pointRadius: spec.labels.length > 24 ? 0 : 3,
        })),
      },
      options: {
        indexAxis: spec.horizontal ? "y" : "x",
        maintainAspectRatio: false,
        animation: matchMedia("(prefers-reduced-motion: reduce)").matches ? false : { duration: 400 },
        plugins: {
          legend: { display: spec.series.length > 1, labels: { color: text, boxWidth: 12, font: { family: cssVar("--font-sans") } } },
          tooltip: { callbacks: { label: (ctx) => ` ${ctx.dataset.label}: ${formatNumber(ctx.parsed[spec.horizontal ? "x" : "y"])}` } },
        },
        scales: {
          // Only the value axis gets a number formatter; setting callback on the category axis
          // (even to undefined) would replace Chart.js's label lookup and print indexes.
          [spec.horizontal ? "x" : "y"]: {
            grid: { color: grid },
            ticks: { color: text, callback: (v) => formatNumber(v) },
            beginAtZero: spec.type === "bar",
          },
          [spec.horizontal ? "y" : "x"]: { grid: { color: "transparent" }, ticks: { color: text } },
        },
      },
    });
  };

  const handle = {
    rerender: () => { if (canvas.isConnected) draw(); else { chart?.destroy(); charts.delete(handle); } },
  };
  charts.add(handle);
  requestAnimationFrame(draw); // draw once attached, so Chart.js can measure the box
  return box;
}

function renderTable(columns, rows) {
  const numeric = columns.map((_, i) => rows.every((r) => r[i] === null || typeof r[i] === "number"));
  const head = el("tr", {}, columns.map((c, i) => el("th", { class: numeric[i] ? "num" : null, title: c }, humanize(c))));
  const body = rows.map((row) => el("tr", {}, row.map((v, i) => el("td", { class: numeric[i] ? "num" : null }, formatCell(v)))));
  return el("div", { class: "table-wrap" }, el("table", {}, el("thead", {}, head), el("tbody", {}, body)));
}

// ---------- Answers ----------

function renderNotice(kind, tag, text) {
  return el("div", { class: `answer ${kind}` }, el("div", { class: "answer-body" },
    el("div", { class: "answer-tag" }, tag),
    el("p", { class: "explanation" }, text)));
}

function renderAnswer(data, question) {
  if (data.status === "clarification") return renderNotice("clarify", "Needs a detail", data.clarification);
  if (data.status === "error") return renderNotice("error", "Couldn't answer", data.error || "Something went wrong.");

  const body = el("div", { class: "answer-body" }, el("p", { class: "explanation" }, data.explanation));
  const view = el("div", { class: "view" });
  const toolbar = el("div", { class: "answer-toolbar" });
  const spec = typeof Chart !== "undefined" ? chartSpec(data.columns, data.rows) : null;

  let showView;
  if (data.row_count === 0) {
    view.append(el("p", { class: "stat-label" }, "The query ran successfully but returned no rows."));
  } else if (data.row_count === 1 && data.columns.length === 1) {
    view.append(el("div", { class: "stat" },
      el("span", { class: "stat-value" }, formatCell(data.rows[0][0])),
      el("span", { class: "stat-label" }, humanize(data.columns[0]))));
  } else if (spec) {
    const seg = el("div", { class: "seg", role: "group", "aria-label": "Result view" });
    const views = { Chart: () => renderChart(spec), Table: () => renderTable(data.columns, data.rows) };
    showView = (name) => {
      view.replaceChildren(views[name]());
      for (const b of seg.children) b.setAttribute("aria-pressed", String(b.textContent === name));
    };
    for (const name of Object.keys(views)) seg.append(el("button", { type: "button", onclick: () => showView(name) }, name));
    toolbar.append(seg);
  } else {
    view.append(renderTable(data.columns, data.rows));
  }

  const sqlPanel = el("div", { class: "sql-panel", hidden: true }, highlightSql(formatSql(data.sql)));
  const sqlBtn = el("button", { type: "button", class: "tool-btn", "aria-expanded": "false" }, "Show SQL");
  sqlBtn.addEventListener("click", () => {
    sqlPanel.hidden = !sqlPanel.hidden;
    sqlBtn.textContent = sqlPanel.hidden ? "Show SQL" : "Hide SQL";
    sqlBtn.setAttribute("aria-expanded", String(!sqlPanel.hidden));
  });
  const copyBtn = el("button", { type: "button", class: "tool-btn" }, "Copy SQL");
  copyBtn.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(data.sql);
      copyBtn.textContent = "Copied ✓";
    } catch {
      copyBtn.textContent = "Copy failed";
    }
    setTimeout(() => (copyBtn.textContent = "Copy SQL"), 1500);
  });
  toolbar.append(el("span", { class: "toolbar-spacer" }), sqlBtn, copyBtn);
  if (data.row_count > 0) {
    toolbar.append(el("button", { type: "button", class: "tool-btn", onclick: () => downloadCsv(data.columns, data.rows, question) }, "Download CSV"));
  }
  body.append(toolbar, sqlPanel, view);
  if (showView) showView("Chart");

  const meta = el("div", { class: "answer-meta" },
    el("span", {}, `${data.row_count} row${data.row_count === 1 ? "" : "s"}${data.truncated ? " (first rows only)" : ""}`),
    el("span", {}, `${(data.latency_ms / 1000).toFixed(1)}s`),
    data.attempts > 1 ? el("span", {}, `fixed its own SQL ${data.attempts === 2 ? "once" : `${data.attempts - 1} times`}`) : null,
    el("span", { class: "toolbar-spacer" }),
    data.model ? el("span", { class: "badge", title: "Model that wrote this query" }, data.model.split("/").pop().replace(/:free$/, "")) : null);

  return el("div", { class: "answer" }, body, meta);
}

// ---------- Network ----------

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
  chat.append(el("div", { class: "msg-user" }, question));

  const started = Date.now();
  const status = el("span", { class: "thinking-steps" }, "Writing and checking SQL…");
  const thinking = el("div", { class: "answer" }, el("div", { class: "answer-body thinking" }, el("span", { class: "spinner" }), status));
  const timer = setInterval(() => {
    const s = Math.round((Date.now() - started) / 1000);
    status.textContent = `Writing and checking SQL… ${s}s`;
  }, 1000);
  chat.append(thinking);
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
    thinking.replaceWith(renderAnswer(data, question));
    // Clarifications stay in history too, so "4 and above" makes sense as the next message.
    history.push({ question, sql: data.status === "ok" ? data.sql : null });
    history = history.slice(-MAX_HISTORY);
  } catch (err) {
    if (myConversation !== conversationId) return;
    let message = err.message;
    if (err.name === "TimeoutError") message = "The request took too long. Please try again.";
    else if (err instanceof TypeError) message = "Could not reach the server. Is it running?";
    thinking.replaceWith(renderAnswer({ status: "error", error: message }));
  } finally {
    clearInterval(timer);
    if (myConversation === conversationId) {
      sendBtn.disabled = false;
      input.focus();
      scrollToBottom();
    }
  }
}

// ---------- Side panels ----------

async function loadSchema() {
  const container = document.getElementById("schema");
  try {
    const tables = await fetchJson(`/api/schema?role=${encodeURIComponent(roleSelect.value)}`);
    container.replaceChildren(...tables.map((t) =>
      el("details", {},
        el("summary", {}, t.name),
        t.description ? el("p", { class: "desc" }, t.description) : null,
        el("ul", {}, t.columns.map((c) => el("li", {}, el("b", {}, c.name), ` ${c.type.toLowerCase()}`))))));
  } catch (err) {
    container.replaceChildren(el("p", { class: "desc" }, `Could not load tables: ${err.message}`));
  }
}

async function loadExamples() {
  const container = document.getElementById("examples");
  try {
    const groups = await fetchJson(`/api/examples?role=${encodeURIComponent(roleSelect.value)}`);
    container.replaceChildren(...groups.map((g) =>
      el("div", {},
        el("h3", { class: "group-title" }, g.group),
        g.questions.map((q) => el("button", { class: "starter", type: "button", onclick: () => ask(q) }, q)))));
  } catch {
    container.replaceChildren();
  }
}

async function loadKpis() {
  const container = document.getElementById("kpis");
  container.replaceChildren(...[1, 2, 3, 4].map(() =>
    el("div", { class: "kpi skeleton" }, el("div", { class: "kpi-label" }, " "), el("div", { class: "kpi-value" }, "0"))));
  try {
    const kpis = await fetchJson("/api/kpis");
    container.replaceChildren(...kpis.map((k) =>
      el("div", { class: "kpi" },
        el("div", { class: "kpi-label" }, k.label),
        el("div", { class: "kpi-value" }, k.value == null ? "—" : formatNumber(k.value), k.unit ? el("span", { class: "kpi-unit" }, k.unit) : null),
        el("div", { class: "kpi-hint" }, k.hint))));
  } catch {
    container.replaceChildren();
  }
}

async function loadStatus() {
  const dot = document.getElementById("statusDot");
  const text = document.getElementById("statusText");
  try {
    const h = await fetchJson("/api/health");
    dot.className = "status-dot ok";
    const provider = { openrouter: "OpenRouter", gemini: "Gemini" }[h.provider] || h.provider;
    text.textContent = `${h.dialect === "mysql" ? "MySQL" : h.dialect} · ${provider}`;
  } catch {
    dot.className = "status-dot bad";
    text.textContent = "Database unreachable";
  }
}

// ---------- Conversation controls ----------

function newConversation(note) {
  conversationId += 1;
  inFlight?.abort();
  sendBtn.disabled = false;
  history = [];
  for (const chart of charts) chart.rerender(); // drops charts that are no longer on screen
  welcome.querySelector(".system-note")?.remove();
  chat.replaceChildren(welcome);
  if (note) welcome.prepend(el("p", { class: "system-note" }, note));
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
  newConversation(`Switched to ${roleSelect.selectedOptions[0].text}. New conversation started.`);
  loadSchema();
  loadExamples();
});

document.getElementById("newChat").addEventListener("click", () => newConversation());

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
loadKpis();
loadStatus();
