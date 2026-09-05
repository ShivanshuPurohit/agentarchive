"use strict";

const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];
const rank = {summary: 0, normal: 1, thinking: 2, verbose: 3};
const modes = ["normal", "thinking", "verbose", "summary"];
const state = {
  tab: "sessions",
  mode: "normal",
  source: "",
  page: null,
  focus: 0,
  listGeneration: 0,
  openGeneration: 0,
  groups: new Map(),
  loaded: 0,
  total: 0
};
function element(tag, className, text) {
  const value = document.createElement(tag);
  if (className) value.className = className;
  if (text !== undefined) value.textContent = text;
  return value;
}
async function api(path) {
  const response = await fetch(path, {credentials: "same-origin"});
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || "Request failed");
  return value;
}
let toastTimer;
function toast(message) {
  const value = $("#toast");
  value.textContent = message;
  value.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => value.classList.add("hidden"), 3000);
}
function appendInline(parent, text) {
  const pattern = /(`[^`\n]+`|\*\*[^*\n]+\*\*|\*[^*\n]+\*)/g;
  let offset = 0;
  let match;
  while ((match = pattern.exec(text))) {
    parent.append(document.createTextNode(text.slice(offset, match.index)));
    const token = match[0];
    const code = token.startsWith("`");
    const strong = token.startsWith("**");
    const value = element(code ? "code" : strong ? "strong" : "em");
    value.textContent = token.slice(code ? 1 : strong ? 2 : 1, code ? -1 : strong ? -2 : -1);
    parent.append(value);
    offset = pattern.lastIndex;
  }
  parent.append(document.createTextNode(text.slice(offset)));
}
function rich(text) {
  const root = element("div", "rich");
  const lines = String(text || "").split("\n");
  let fence = null;
  let list = null;
  let listType = "";

  function finishFence() {
    if (!fence) return;
    const pre = element("pre");
    pre.textContent = fence.join("\n");
    root.append(pre);
    fence = null;
  }

  for (const line of lines) {
    if (line.startsWith("```")) {
      if (fence) finishFence();
      else fence = [];
      list = null;
      continue;
    }
    if (fence) {
      fence.push(line);
      continue;
    }
    if (!line.trim()) {
      list = null;
      listType = "";
      continue;
    }
    if (/^\s*(?:[-*_]\s*){3,}$/.test(line)) {
      root.append(element("hr"));
      list = null;
      continue;
    }

    const bullet = line.match(/^\s*[-*]\s+(.*)$/);
    const numbered = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (bullet || numbered) {
      const type = numbered ? "ol" : "ul";
      if (!list || listType !== type) {
        list = element(type);
        listType = type;
        root.append(list);
      }
      const item = element("li");
      appendInline(item, (bullet || numbered)[1]);
      list.append(item);
      continue;
    }

    list = null;
    listType = "";
    let block;
    if (/^#{1,4}\s/.test(line)) {
      block = element("h3");
      appendInline(block, line.replace(/^#{1,4}\s+/, ""));
    } else if (/^>\s?/.test(line)) {
      block = element("blockquote");
      appendInline(block, line.replace(/^>\s?/, ""));
    } else {
      block = element("p");
      appendInline(block, line);
    }
    root.append(block);
  }
  finishFence();
  return root;
}
function visible(item) {
  return rank[item.visibility || "verbose"] <= rank[state.mode];
}
async function extend(button, target, ref, offset, asRich) {
  try {
    const path = `/api/transcripts/${encodeURIComponent(state.source)}/item?line=${ref.line}&index=${ref.index}&offset=${offset}&limit=65536`;
    const value = await api(path);
    target.append(asRich ? rich(value.text) : document.createTextNode(value.text));
    if (value.next_offset) {
      button.textContent = `Load more · ${value.next_offset.toLocaleString()} / ${value.char_count.toLocaleString()}`;
      button.onclick = () => extend(button, target, ref, value.next_offset, asRich);
    } else {
      button.remove();
    }
  } catch (error) {
    toast(error.message);
  }
}
function addLoadMore(item, target, asRich = true) {
  if (!item.truncated) return;
  const button = element("button", "load-more", `Load full content · ${item.char_count.toLocaleString()} chars`);
  button.type = "button";
  button.onclick = () => extend(button, target, item.ref, item.text_preview.length, asRich);
  target.append(button);
}
function payload(part) {
  const wrapper = element("div", "component");
  wrapper.append(element("div", "component-label", part.phase || part.kind));
  const pre = element("pre", "payload", part.text_preview || "");
  wrapper.append(pre);
  addLoadMore(part, pre, false);
  return wrapper;
}
function renderMessage(item) {
  const event = element("section", "event");
  if (item.role === "user") {
    const row = element("div", "user-row");
    const bubble = element("div", "user-bubble");
    bubble.append(rich(item.text_preview));
    addLoadMore(item, bubble);
    row.append(bubble);
    event.append(row);
  } else {
    const body = element("div", "assistant");
    body.append(rich(item.text_preview));
    addLoadMore(item, body);
    event.append(body);
  }
  return event;
}
function renderTool(item) {
  const details = element("details", "activity-row");
  const summary = element("summary");
  summary.append(
    element("span", `status ${item.status || "pending"}`),
    element("span", "activity-title", item.title || item.tool_name || "Tool call")
  );
  if (item.tool_name && !String(item.title || "").startsWith(item.tool_name)) {
    summary.append(element("span", "tool-kind", item.tool_name));
  }
  details.append(summary);
  const content = element("div", "activity-content");
  for (const part of item.components || []) {
    if (visible(part)) content.append(payload(part));
  }
  details.append(content);
  return details;
}

function activitySummary(items) {
  const labels = items.map(item => `${item.tool_name || ""} ${item.title || ""}`.toLowerCase());
  const commands = labels.filter(value => /bash|shell|exec|command/.test(value)).length;
  const reads = labels.filter(value => /\bread\b|view|open file/.test(value)).length;
  const edits = labels.filter(value => /edit|patch|write|create file/.test(value)).length;
  const failed = items.filter(item => item.status === "error").length;
  const parts = [];
  if (commands) parts.push(`${commands} command${commands === 1 ? "" : "s"}`);
  if (reads) parts.push(`${reads} read${reads === 1 ? "" : "s"}`);
  if (edits) parts.push(`${edits} edit${edits === 1 ? "" : "s"}`);
  if (!parts.length) parts.push(`${items.length} tool call${items.length === 1 ? "" : "s"}`);
  if (failed) parts.push(`${failed} failed`);
  return parts.join(" · ");
}

function renderToolGroup(items) {
  const group = element("section", "event activity-group");
  const focused = items.find(item => state.focus && item.line === state.focus);
  if (focused) {
    group.classList.add("focused");
    group.dataset.line = focused.line;
  }
  if (items.length > 1 || items.some(item => item.status === "error")) {
    group.append(element("div", "activity-summary", activitySummary(items)));
  }
  const list = element("div", "activity-list");
  for (const item of items) list.append(renderTool(item));
  group.append(list);
  return group;
}
function renderReasoning(item) {
  const event = element("section", "event");
  const details = element("details", "thought");
  details.open = state.mode === "thinking" || state.mode === "verbose";
  const summary = element("summary", "", item.title || "Thinking");
  if (item.opaque) summary.append(element("span", "opaque", "encrypted — plaintext unavailable"));
  details.append(summary);
  const body = element("div", "reasoning-body");
  body.append(rich(item.text_preview));
  addLoadMore(item, body);
  details.append(body);
  event.append(details);
  return event;
}
function renderBoundary(item) {
  const event = element("section", "event");
  const boundary = element("div", "boundary");
  boundary.append(element("span", "", item.text_preview || item.title || "Session boundary"));
  event.append(boundary);
  return event;
}
function renderMetadata(item) {
  const event = element("section", "event");
  const details = element("details", "metadata-card");
  details.append(element("summary", "", item.title || item.kind));
  const body = element("div", "metadata-body");
  body.append(payload(item));
  details.append(body);
  event.append(details);
  return event;
}
function renderItem(item) {
  let event;
  if (item.kind === "message") event = renderMessage(item);
  else if (item.kind === "reasoning") event = renderReasoning(item);
  else if (item.kind === "boundary") event = renderBoundary(item);
  else event = renderMetadata(item);
  event.dataset.line = item.line;
  if (state.focus && item.line === state.focus) event.classList.add("focused");
  return event;
}

function renderTranscript() {
  const transcript = $("#transcript");
  transcript.replaceChildren();
  if (!state.page) return;
  let tools = [];
  const flushTools = () => {
    if (!tools.length) return;
    transcript.append(renderToolGroup(tools));
    tools = [];
  };
  for (const item of state.page.items) {
    if (!visible(item)) continue;
    if (item.kind === "tool") {
      tools.push(item);
      continue;
    }
    flushTools();
    transcript.append(renderItem(item));
  }
  flushTools();

  $("#empty").classList.add("hidden");
  $$(".page-nav").forEach(value => value.classList.remove("hidden"));
  $("#range").textContent = `${state.page.from_line.toLocaleString()}–${state.page.to_line.toLocaleString()} of ${state.page.display_line_count.toLocaleString()} stored lines`;
  $$("[data-page=previous]").forEach(button => button.disabled = !state.page.previous_line);
  $$("[data-page=next]").forEach(button => button.disabled = !state.page.next_line);
  requestAnimationFrame(() => $(".event.focused")?.scrollIntoView({block: "center"}));
}

function shortProject(project) {
  const cleaned = String(project || "").replace(/^\/(?:Users|home)\/[^/]+(?=\/|$)/, "~").replace(/\/$/, "");
  if (!cleaned) return "";
  const parts = cleaned.split("/").filter(Boolean);
  return parts.at(-1) || cleaned;
}

function formatDate(value, includeTime = false) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const now = new Date();
  const sameDay = date.toDateString() === now.toDateString();
  if (sameDay) return date.toLocaleTimeString([], {hour: "numeric", minute: "2-digit"});
  const options = {month: "short", day: "numeric"};
  if (date.getFullYear() !== now.getFullYear()) options.year = "numeric";
  if (includeTime) {
    options.hour = "numeric";
    options.minute = "2-digit";
  }
  return date.toLocaleDateString([], options);
}

function markActiveSource() {
  $$(".source-card[data-source]").forEach(button => {
    button.classList.toggle("active", button.dataset.source === state.source);
  });
}

async function openSource(id, line = 0, from = 1) {
  const generation = ++state.openGeneration;
  state.source = id;
  state.focus = Number(line) || 0;
  markActiveSource();
  $("#transcript").setAttribute("aria-busy", "true");
  try {
    const query = new URLSearchParams({from_line: from, limit: 120});
    if (state.focus) query.set("focus_line", state.focus);
    const page = await api(`/api/transcripts/${encodeURIComponent(id)}?${query}`);
    if (generation !== state.openGeneration) return;
    state.page = page;
    state.focus = page.focus_line || state.focus;
    const source = page.source;
    $("#sessionTitle").textContent = page.title || source.title || source.session_id;
    $("#harnessBadge").textContent = source.harness;
    const metadata = [
      shortProject(source.project || page.cwd),
      page.model || source.model,
      formatDate(source.updated, true)
    ].filter(Boolean);
    $("#sessionMeta").textContent = metadata.join(" · ");
    const freshness = page.freshness;
    $("#freshness").textContent = freshness.live_delta ? "Live · capture pending" : "Captured";
    $("#freshness").classList.toggle("live", Boolean(freshness.live_delta));
    history.replaceState(null, "", `#source=${encodeURIComponent(id)}${state.focus ? `&line=${state.focus}` : ""}`);
    renderTranscript();
    markActiveSource();
    if (matchMedia("(max-width: 840px)").matches) setSidebar(false);
  } catch (error) {
    if (generation === state.openGeneration) toast(error.message);
  } finally {
    $("#transcript").removeAttribute("aria-busy");
  }
}

function projectIdentity(source) {
  const path = String(source.project || "").replace(/\/$/, "");
  if (!path) return {key: `unscoped:${source.harness}`, label: `${source.harness} archive`, path: ""};
  return {key: path, label: shortProject(path), path};
}

function ensureProjectGroup(source) {
  const identity = projectIdentity(source);
  if (state.groups.has(identity.key)) return state.groups.get(identity.key);
  const section = element("section", "project-group");
  section.dataset.project = identity.key;
  const header = element("button", "project-header");
  header.type = "button";
  header.title = identity.path || identity.label;
  header.setAttribute("aria-expanded", "true");
  header.append(
    element("span", "project-chevron"),
    element("span", "project-name", identity.label),
    element("span", "project-count", "0")
  );
  const list = element("div", "project-sessions");
  header.onclick = () => {
    const collapsed = section.classList.toggle("collapsed");
    header.setAttribute("aria-expanded", String(!collapsed));
  };
  section.append(header, list);
  $("#sourceList").append(section);
  const group = {section, list, count: 0};
  state.groups.set(identity.key, group);
  return group;
}

function sourceCard(source) {
  const button = element("button", "source-card");
  button.type = "button";
  button.dataset.source = source.source_id;
  button.setAttribute("role", "listitem");
  button.title = source.title || source.session_id;
  button.append(element("span", "source-title", source.title || source.session_id));
  const detail = element("span", "source-detail");
  detail.append(
    element("span", "harness", source.harness),
    element("span", "", formatDate(source.updated)),
    element("span", "", source.model || "")
  );
  button.append(detail);
  button.onclick = () => openSource(source.source_id);
  return button;
}

function appendSession(source) {
  const group = ensureProjectGroup(source);
  group.count += 1;
  group.section.querySelector(".project-count").textContent = group.count.toLocaleString();
  group.list.append(sourceCard(source));
}

function hitCard(hit) {
  const button = element("button", `source-card hit${hit.source_id ? "" : " unrouted"}`);
  button.type = "button";
  const label = `${hit.harness || "archive"} · ${hit.kind || hit.role || hit.source || "evidence"}`;
  button.append(element("span", "source-title", label), element("span", "excerpt", hit.excerpt || ""));
  if (hit.source_id) button.onclick = () => openSource(hit.source_id, hit.line_no || 1);
  return button;
}

async function loadSessions() {
  const generation = ++state.listGeneration;
  const list = $("#sourceList");
  list.replaceChildren();
  state.groups = new Map();
  state.loaded = 0;
  state.total = 0;
  $("#moreButton").classList.add("hidden");
  let cursor = "";
  let first = null;
  try {
    do {
      const query = new URLSearchParams({
        limit: 100,
        q: $("#searchInput").value,
        harness: $("#harnessFilter").value,
        children: $("#childrenFilter").checked ? "1" : "0"
      });
      if (cursor) query.set("cursor", cursor);
      const value = await api(`/api/sessions?${query}`);
      if (generation !== state.listGeneration) return;
      state.total = value.total;
      first ||= value.sessions[0] || null;
      for (const source of value.sessions) appendSession(source);
      state.loaded += value.sessions.length;
      cursor = value.next_cursor || "";
      $("#listMeta").textContent = cursor
        ? `Loading ${state.loaded.toLocaleString()} of ${state.total.toLocaleString()} chats…`
        : `${state.total.toLocaleString()} chats · Claude + Codex`;
      if (!state.source && first) openSource(first.source_id);
      await new Promise(resolve => setTimeout(resolve, 0));
    } while (cursor && generation === state.listGeneration);
    markActiveSource();
  } catch (error) {
    if (generation !== state.listGeneration) return;
    $("#listMeta").textContent = `Loaded ${state.loaded.toLocaleString()} chats · interrupted`;
    $("#moreButton").textContent = "Retry loading";
    $("#moreButton").classList.remove("hidden");
    toast(error.message);
  }
}

async function loadEvidence() {
  const generation = ++state.listGeneration;
  const list = $("#sourceList");
  list.replaceChildren();
  $("#moreButton").classList.add("hidden");
  const query = $("#searchInput").value.trim();
  if (!query) {
    $("#listMeta").textContent = "Search errors, decisions, tools, or reasoning";
    return;
  }
  try {
    const value = await api(`/api/search?q=${encodeURIComponent(query)}&limit=50`);
    if (generation !== state.listGeneration) return;
    for (const hit of value.hits) list.append(hitCard(hit));
    $("#listMeta").textContent = `${value.hits.length} evidence hit${value.hits.length === 1 ? "" : "s"}`;
  } catch (error) {
    if (generation === state.listGeneration) toast(error.message);
  }
}

function loadList() {
  return state.tab === "sessions" ? loadSessions() : loadEvidence();
}

function setMode(mode) {
  if (!rank.hasOwnProperty(mode)) return;
  state.mode = mode;
  document.body.dataset.mode = mode;
  $("#modeLabel").textContent = mode[0].toUpperCase() + mode.slice(1);
  $$("#modeMenu [data-mode]").forEach(button => {
    button.classList.toggle("active", button.dataset.mode === mode);
  });
  $("#modeMenu").classList.add("hidden");
  $("#modeButton").setAttribute("aria-expanded", "false");
  localStorage.setItem("agentarchive.transcriptMode", mode);
  renderTranscript();
}

function setSidebar(open) {
  document.body.classList.toggle("sidebar-visible", open);
}

let debounce;
$("#searchInput").oninput = () => {
  if (state.tab !== "sessions") return;
  clearTimeout(debounce);
  debounce = setTimeout(loadList, 180);
};
$("#searchInput").onkeydown = event => {
  if (event.key === "Enter") loadList();
};
$("#harnessFilter").onchange = $("#childrenFilter").onchange = loadList;
$("#moreButton").onclick = loadList;
$("#openSidebar").onclick = () => setSidebar(true);
$("#closeSidebar").onclick = $("#sidebarScrim").onclick = () => setSidebar(false);
$("#newSearchButton").onclick = () => {
  state.tab = "sessions";
  $$("[data-tab]").forEach(button => button.classList.toggle("active", button.dataset.tab === "sessions"));
  $("#searchInput").value = "";
  $("#searchInput").placeholder = "Search sessions";
  $("#searchInput").setAttribute("aria-label", "Search sessions");
  $(".filters").classList.remove("hidden");
  loadList();
};

$$("[data-tab]").forEach(button => {
  button.onclick = () => {
    state.tab = button.dataset.tab;
    $$("[data-tab]").forEach(value => value.classList.toggle("active", value === button));
    const sessions = state.tab === "sessions";
    $("#searchInput").value = "";
    $("#searchInput").placeholder = sessions ? "Search sessions" : "Search all evidence";
    $("#searchInput").setAttribute("aria-label", sessions ? "Search sessions" : "Search all evidence");
    $(".filters").classList.toggle("hidden", !sessions);
    loadList();
    $("#searchInput").focus();
  };
});

$("#modeButton").onclick = () => {
  const menu = $("#modeMenu");
  menu.classList.toggle("hidden");
  $("#modeButton").setAttribute("aria-expanded", String(!menu.classList.contains("hidden")));
};
$$("#modeMenu [data-mode]").forEach(button => button.onclick = () => setMode(button.dataset.mode));
$$("[data-page]").forEach(button => {
  button.onclick = () => {
    const line = state.page?.[`${button.dataset.page}_line`];
    if (!line) return;
    state.focus = 0;
    openSource(state.source, 0, line);
    $("#main").scrollTo({top: 0, behavior: "smooth"});
  };
});

document.addEventListener("click", event => {
  if (!event.target.closest("#modeMenu, #modeButton")) {
    $("#modeMenu").classList.add("hidden");
    $("#modeButton").setAttribute("aria-expanded", "false");
  }
});

document.addEventListener("keydown", event => {
  if (event.ctrlKey && event.key.toLowerCase() === "o") {
    event.preventDefault();
    setMode(modes[(modes.indexOf(state.mode) + 1) % modes.length]);
  }
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    setSidebar(true);
    $("#searchInput").focus();
  }
  if (event.key === "/" && !/input|textarea|select/i.test(document.activeElement?.tagName || "")) {
    event.preventDefault();
    setSidebar(true);
    $("#searchInput").focus();
  }
  if (event.key === "Escape") {
    $("#modeMenu").classList.add("hidden");
    $("#modeButton").setAttribute("aria-expanded", "false");
    if (matchMedia("(max-width: 840px)").matches) setSidebar(false);
  }
});

async function boot() {
  if (matchMedia("(max-width: 840px)").matches) setSidebar(false);
  setMode(localStorage.getItem("agentarchive.transcriptMode") || "normal");
  const hash = new URLSearchParams(location.hash.slice(1));
  if (hash.get("source")) await openSource(hash.get("source"), hash.get("line") || 0);
  await loadList();
}

boot();
