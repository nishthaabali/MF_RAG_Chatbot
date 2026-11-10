/* Mutual Fund FAQ Assistant: chat UI (implementation plan, Phase 9).
 *
 * Talks to the FastAPI backend at window.MF_API_BASE (config.js; "/api" = same origin):
 *   GET  /api/meta, GET /api/health
 *   POST /api/threads, GET|DELETE /api/threads/{id}, POST /api/threads/{id}/messages
 *
 * Privacy: localStorage holds only thread ids, server-made titles and timestamps. Titles are never
 * taken from what the user typed, because the server replaces a message containing PII with a
 * placeholder and builds the title from that.
 */
(function () {
  "use strict";

  // Set by config.js (same origin by default; the Railway URL on Vercel), without a trailing /.
  const API = String(window.MF_API_BASE || "/api").replace(/\/+$/, "");
  const WITHHELD = "[message withheld: contained personal information]";
  const GENERIC_ERROR = "Something went wrong — please try again.";
  const STORE_KEY = "mfqa.threads.v1";
  const ACTIVE_KEY = "mfqa.active.v1";
  const DEFAULT_EXAMPLES = [
    "What is the expense ratio of Parag Parikh Flexi Cap Fund?",
    "What is the exit load of Kotak Multi Cap Fund?",
    "How do I download my capital gains statement?",
  ];

  // How each reply type is presented. Refusals and notices are "info", not answers.
  const KINDS = {
    answer:              { label: "Answer",                 badge: "From source",       tone: "" },
    comparison:          { label: "Side by side",           badge: "Published figures", tone: "" },
    greeting:            { label: "Hello",                  badge: "",                  tone: "" },
    advisory_refusal:    { label: "Facts only",             badge: "No advice",         tone: "info" },
    performance_refusal: { label: "Performance",            badge: "No judgements",     tone: "info" },
    out_of_scope:        { label: "Outside my scope",       badge: "",                  tone: "info" },
    not_covered:         { label: "Scheme not covered",     badge: "",                  tone: "info" },
    no_evidence:         { label: "Not found in sources",   badge: "",                  tone: "info" },
    clarify:             { label: "Which scheme?",          badge: "Need detail",       tone: "info" },
    pii_blocked:         { label: "Privacy",                badge: "Message withheld",  tone: "privacy" },
    unavailable:         { label: "Temporarily unavailable", badge: "",                 tone: "warn" },
  };

  // ------------------------------------------------------------------ storage (never required)
  const storage = {
    get(key, fallback) {
      try {
        const raw = window.localStorage.getItem(key);
        return raw == null ? fallback : JSON.parse(raw);
      } catch (_) { return fallback; }
    },
    set(key, value) {
      try { window.localStorage.setItem(key, JSON.stringify(value)); } catch (_) { /* private mode */ }
    },
    remove(key) {
      try { window.localStorage.removeItem(key); } catch (_) { /* ignore */ }
    },
  };

  // ------------------------------------------------------------------ state
  const state = {
    meta: null,
    maxChars: 500,
    threads: [],             // [{id, title, updatedAt, pinned}] newest first
    collapsed: false,        // desktop sidebar collapsed to a rail
    activeId: null,          // null = a new chat not yet created on the server
    messages: new Map(),     // thread id -> [{role, content, type, citation_url, last_updated, time, withheld}]
    loaded: new Set(),       // thread ids whose history came from the server
    pending: new Set(),      // thread ids waiting for a reply
    creating: false,         // a new thread is being created for the first message
    errors: new Map(),       // thread id -> {text, retryText}
    search: "",
    scopeGroup: "All",
    scopeFilter: "",
  };

  const $ = (id) => document.getElementById(id);
  const el = {
    sidebar: $("sidebar"), scope: $("scope"), scrim: $("scrim"),
    openSidebar: $("open-sidebar"), openScope: $("open-scope"),
    newChat: $("new-chat"), threadSearch: $("thread-search"), threadList: $("thread-list"),
    statusDot: $("status-dot"), statusTitle: $("status-title"), statusBody: $("status-body"),
    chatTitle: $("chat-title"), deleteCurrent: $("delete-current"), disclaimer: $("disclaimer-text"),
    app: $("app"), collapse: $("collapse-sidebar"), chatHeading: $("chat-heading"),
    renameCurrent: $("rename-current"), pinCurrent: $("pin-current"),
    pinCurrentLabel: $("pin-current-label"), exportCurrent: $("export-current"), menu: $("thread-menu"),
    scroll: $("chat-scroll"), welcome: $("welcome"), welcomeText: $("welcome-text"),
    welcomeTitle: $("welcome-title"), examples: $("examples"), messages: $("messages"),
    composer: $("composer"), input: $("composer-input"), send: $("send"),
    charCount: $("char-count"), piiText: $("pii-text"),
    scopeCount: $("scope-count"), scopeFilter: $("scope-filter"), scopeChips: $("scope-chips"),
    schemeList: $("scheme-list"), toast: $("toast"),
  };

  // ------------------------------------------------------------------ helpers
  function h(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children.flat()) if (c != null) node.append(c);
    return node;
  }

  function icon(name, cls) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "icon" + (cls ? " " + cls : ""));
    svg.setAttribute("aria-hidden", "true");
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", "#i-" + name);
    svg.append(use);
    return svg;
  }

  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  /** "2026-11-09" -> "09 Nov 2026" (same format as the server's footer; no time-zone shift). */
  function formatDate(iso) {
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || "");
    return m ? `${m[3]} ${MONTHS[Number(m[2]) - 1]} ${m[1]}` : "";
  }

  function formatTime(date) {
    try { return date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }); }
    catch (_) { return ""; }
  }

  function safeUrl(url) {
    try {
      const u = new URL(url);
      return u.protocol === "https:" || u.protocol === "http:" ? u : null;
    } catch (_) { return null; }
  }

  function shortUrl(u) {
    const path = u.pathname.replace(/\/$/, "");
    const tail = path.length > 28 ? "…" + path.slice(-26) : path;
    return u.hostname.replace(/^www\./, "") + tail;
  }

  let toastTimer = null;
  function toast(text) {
    el.toast.textContent = text;
    el.toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.toast.hidden = true; }, 3200);
  }

  class ApiError extends Error {
    constructor(status, detail) { super(detail || "HTTP " + status); this.status = status; this.detail = detail; }
  }

  async function api(path, options) {
    let res;
    try {
      res = await fetch(API + path, {
        ...options,
        headers: options && options.body ? { "Content-Type": "application/json" } : undefined,
      });
    } catch (_) {
      throw new ApiError(0, null); // network failure
    }
    if (res.status === 204) return null;
    let body = null;
    try { body = await res.json(); } catch (_) { /* empty or not JSON */ }
    if (!res.ok) {
      const detail = body && typeof body.detail === "string" ? body.detail : null;
      throw new ApiError(res.status, detail);
    }
    return body;
  }

  // ------------------------------------------------------------------ thread list
  function saveThreads() {
    storage.set(STORE_KEY, state.threads.map(({ id, title, updatedAt, pinned }) =>
      ({ id, title, updatedAt, pinned: !!pinned })));
    if (state.activeId) storage.set(ACTIVE_KEY, state.activeId); else storage.remove(ACTIVE_KEY);
  }

  function loadThreads() {
    const raw = storage.get(STORE_KEY, []);
    state.threads = Array.isArray(raw)
      ? raw.filter((t) => t && typeof t.id === "string")
          .map((t) => ({ id: t.id, title: typeof t.title === "string" ? t.title : null,
                         updatedAt: Number(t.updatedAt) || Date.now(), pinned: t.pinned === true }))
      : [];
    sortThreads();
  }

  function sortThreads() { state.threads.sort((a, b) => b.updatedAt - a.updatedAt); }
  function findThread(id) { return state.threads.find((t) => t.id === id); }
  function threadTitle(t) {
    if (!t || !t.title) return "New chat";
    return t.title.startsWith("[message withheld") ? "Message withheld (personal info)" : t.title;
  }

  function touchThread(id, patch) {
    const t = findThread(id);
    if (!t) return;
    Object.assign(t, patch, { updatedAt: Date.now() });
    sortThreads();
    saveThreads();
  }

  function forgetThread(id) {
    state.threads = state.threads.filter((t) => t.id !== id);
    state.messages.delete(id);
    state.loaded.delete(id);
    state.errors.delete(id);
    state.pending.delete(id);
    if (state.activeId === id) state.activeId = null;
    saveThreads();
  }

  function groupOf(ts) {
    const start = new Date(); start.setHours(0, 0, 0, 0);
    if (ts >= start.getTime()) return "Today";
    if (ts >= start.getTime() - 6 * 864e5) return "Previous 7 days";
    return "Older";
  }

  function renderThreadList() {
    const q = state.search.trim().toLowerCase();
    const list = state.threads.filter((t) => !q || threadTitle(t).toLowerCase().includes(q));
    el.threadList.replaceChildren();
    if (!list.length) {
      el.threadList.append(h("p", { class: "thread-empty",
        text: q ? "No conversations match." : "Your conversations will appear here." }));
      return;
    }
    const groups = new Map();
    for (const t of list) {
      const g = t.pinned ? "Pinned" : groupOf(t.updatedAt);
      if (!groups.has(g)) groups.set(g, []);
      groups.get(g).push(t);
    }
    const order = ["Pinned", "Today", "Previous 7 days", "Older"];
    const sorted = [...groups].sort((a, b) => order.indexOf(a[0]) - order.indexOf(b[0]));
    for (const [label, items] of sorted) {
      const ul = h("ul", { class: "thread-items", role: "list", style: "list-style:none;margin:0;padding:0" });
      for (const t of items) {
        const active = t.id === state.activeId;
        const title = threadTitle(t);
        ul.append(h("li", { class: "thread-item" + (t.pinned ? " thread-item--pinned" : ""),
                            "aria-current": active ? "true" : null, "data-thread": t.id },
          h("button", { class: "thread-item__open", type: "button", title,
                        onclick: () => openThread(t.id) },
            icon(t.pinned ? "pin" : "chat"),
            h("span", { class: "thread-item__title", text: title }),
            t.pinned ? h("span", { class: "visually-hidden", text: "(pinned)" }) : null,
            state.pending.has(t.id) ? h("span", { class: "thread-item__busy", title: "Waiting for reply" }) : null),
          h("button", { class: "thread-item__more", type: "button", "aria-haspopup": "menu",
                        "aria-expanded": "false", "aria-label": `Actions for chat: ${title}`,
                        title: "Pin, rename, export or delete",
                        onclick: (e) => openMenu(t.id, e.currentTarget) }, icon("more"))));
      }
      el.threadList.append(h("div", { class: "thread-group" },
        h("div", { class: "thread-group__label", text: label }), ul));
    }
  }

  // ------------------------------------------------------------------ chat pane
  function renderHeader() {
    const t = state.activeId ? findThread(state.activeId) : null;
    if (!el.chatHeading.querySelector(".rename-input")) {
      el.chatTitle.textContent = t ? threadTitle(t) : "New conversation";
    }
    for (const b of [el.deleteCurrent, el.renameCurrent, el.pinCurrent, el.exportCurrent]) b.hidden = !t;
    if (t) {
      el.pinCurrent.setAttribute("aria-pressed", String(!!t.pinned));
      el.pinCurrent.title = t.pinned ? "Unpin chat" : "Pin chat";
      el.pinCurrentLabel.textContent = t.pinned ? "Pinned" : "Pin";
    }
    document.title = (t && t.title ? t.title + " · " : "") + "Mutual Fund FAQ Assistant";
  }

  /** Animate a message only the first time it is drawn, not on every re-render. */
  function entering(m) {
    if (m.shown) return "";
    m.shown = true;
    return " msg--new";
  }

  function userBubble(m) {
    return h("div", { class: "msg msg--user" + entering(m) },
      h("div", { class: "bubble-user" + (m.withheld ? " bubble-user--withheld" : "") },
        h("div", { class: "bubble-user__label" },
          h("span", { text: "Your question" }),
          m.time ? h("time", { class: "bubble-user__time", text: formatTime(m.time) }) : null),
        h("p", { class: "bubble-user__text", text: m.content })),
      h("div", { class: "msg__avatar", "aria-hidden": "true", text: "You" }));
  }

  function replyCard(m) {
    const kind = KINDS[m.type] || KINDS.answer;
    const url = safeUrl(m.citation_url);
    const updated = formatDate(m.last_updated);
    const card = h("article", { class: "card" + (kind.tone ? " card--" + kind.tone : ""),
                                "aria-label": kind.label },
      h("div", { class: "card__head" },
        h("span", { class: "card__kind" }, h("span", { class: "card__dot", "aria-hidden": "true" }), kind.label),
        kind.badge ? h("span", { class: "card__badge", text: kind.badge }) : null),
      h("p", { class: "card__text", text: m.content }));

    const table = m.comparison;
    if (table && Array.isArray(table.columns) && table.columns.length) {
      card.append(comparisonTable(table));
      for (const note of table.notes || []) card.append(h("p", { class: "card__note", text: note }));
      const links = table.columns.map((c) => {
        const u = safeUrl(c.url);
        return u ? h("a", { class: "source-link", href: u.href, target: "_blank", rel: "noopener noreferrer",
                           "aria-label": `Source for ${c.name}: ${u.hostname} (opens in a new tab)`, title: u.href },
                     h("span", { class: "source-link__label", text: c.name }),
                     icon("external")) : null;
      });
      card.append(h("div", { class: "card__foot" },
        h("div", { class: "source-list" }, ...links),
        updated ? h("span", { class: "updated" }, icon("calendar"),
                    `Last updated from sources: ${updated}`) : null));
      return h("div", { class: "msg" + entering(m) },
        h("div", { class: "msg__avatar", "aria-hidden": "true" }, icon("logo")), card);
    }

    if (url || updated) {
      card.append(h("div", { class: "card__foot" },
        url ? h("a", { class: "source-link", href: url.href, target: "_blank", rel: "noopener noreferrer",
                       "aria-label": `Source: ${url.hostname} (opens in a new tab)`, title: url.href },
                h("span", { class: "source-link__label", text: "Source" }),
                h("span", { class: "source-link__host", text: shortUrl(url) }),
                icon("external")) : null,
        updated ? h("span", { class: "updated" }, icon("calendar"),
                    `Last updated from sources: ${updated}`) : null));
    }
    return h("div", { class: "msg" + entering(m) },
      h("div", { class: "msg__avatar", "aria-hidden": "true" }, icon("logo")), card);
  }

  // Published values only, in the order the schemes were named: never sorted or highlighted.
  function comparisonTable(table) {
    const head = h("tr", {}, h("th", { scope: "col", text: "Field" }),
      ...table.columns.map((c) => h("th", { scope: "col", text: c.name })));
    const body = (table.rows || []).map((r) =>
      h("tr", {}, h("th", { scope: "row", text: r.label }),
        ...r.cells.map((v) => h("td", { class: v === "—" ? "is-missing" : "", text: v }))));
    return h("div", { class: "compare", role: "region", tabindex: "0",
                      "aria-label": "Published figures side by side" },
      h("table", { class: "compare__table" }, h("thead", {}, head), h("tbody", {}, ...body)));
  }

  function pendingCard() {
    return h("div", { class: "msg", "aria-label": "Assistant is answering" },
      h("div", { class: "msg__avatar", "aria-hidden": "true" }, icon("logo")),
      h("div", { class: "card" },
        h("div", { class: "card__head" }, h("span", { class: "card__kind" },
          h("span", { class: "card__dot", "aria-hidden": "true" }), "Checking sources…")),
        h("div", { class: "typing", "aria-hidden": "true" }, h("span"), h("span"), h("span"))));
  }

  function errorRow(threadId, err) {
    return h("div", { class: "error-row", role: "alert" },
      icon("alert"),
      h("span", { class: "error-row__text", text: err.text }),
      err.retryText ? h("button", { class: "btn", type: "button",
        onclick: () => { state.errors.delete(threadId); send(err.retryText); } }, "Try again") : null);
  }

  function renderChat({ scroll = true } = {}) {
    renderHeader();
    const id = state.activeId;
    const msgs = id ? state.messages.get(id) || [] : [];
    const loading = id && !state.loaded.has(id) && !msgs.length && !state.pending.has(id);
    const err = id ? state.errors.get(id) : state.errors.get(null);

    el.welcome.hidden = !!(msgs.length || loading || (id && state.pending.has(id)));
    const nodes = msgs.map((m) => (m.role === "user" ? userBubble(m) : replyCard(m)));
    if (loading) nodes.push(h("p", { class: "loading-thread", text: "Loading conversation…" }));
    if (id && state.pending.has(id)) nodes.push(pendingCard());
    if (err) nodes.push(errorRow(id, err));
    el.messages.replaceChildren(...nodes);

    updateComposer();
    renderSchemes();
    const hasContent = nodes.length > 0;
    if (scroll) requestAnimationFrame(() => { el.scroll.scrollTop = hasContent ? el.scroll.scrollHeight : 0; });
  }

  function renderWelcome() {
    const meta = state.meta;
    if (meta) {
      el.welcomeText.textContent = meta.welcome;
      if (meta.schemes && meta.schemes.length) {
        el.welcomeTitle.textContent = `Ask a factual question about ${meta.schemes.length} mutual fund schemes`;
      }
      el.disclaimer.textContent = meta.disclaimer || el.disclaimer.textContent;
      el.piiText.textContent = meta.pii_notice || el.piiText.textContent;
    }
    const examples = (meta && meta.examples && meta.examples.length ? meta.examples : DEFAULT_EXAMPLES).slice(0, 3);
    el.examples.replaceChildren(...examples.map((text) => {
      const topic = /expense|ter\b/i.test(text) ? ["Expense ratio", "percent"]
        : /exit load/i.test(text) ? ["Exit load", "door"]
        : /statement|capital gains/i.test(text) ? ["Statements", "receipt"]
        : ["Example", "chat"];
      return h("button", { class: "example", type: "button", role: "listitem",
                           onclick: () => send(text) },
        h("span", { class: "example__top" },
          h("span", { class: "example__icon", "aria-hidden": "true" }, icon(topic[1])),
          h("span", { class: "example__arrow", "aria-hidden": "true" }, icon("arrow"))),
        h("span", { class: "example__label", text: topic[0] }),
        h("span", { class: "example__text", text }));
    }));
  }

  // ------------------------------------------------------------------ composer
  function updateComposer() {
    const len = el.input.value.length;
    const busy = state.activeId ? state.pending.has(state.activeId) : state.creating;
    el.charCount.textContent = `${len} / ${state.maxChars}`;
    el.charCount.classList.toggle("char-count--near", len > state.maxChars * 0.9);
    el.send.disabled = busy || !el.input.value.trim();
    el.send.setAttribute("aria-label", busy ? "Waiting for the reply" : "Send question");
    el.input.setAttribute("aria-busy", busy ? "true" : "false");
  }

  const narrow = window.matchMedia("(max-width: 767px)");
  const LONG_PLACEHOLDER = "Ask about expense ratio, exit load, minimum SIP, riskometer, benchmark…";
  function updatePlaceholder() {
    el.input.placeholder = narrow.matches ? "Ask a factual question…" : LONG_PLACEHOLDER;
  }

  function autosize() {
    el.input.style.height = "auto";
    el.input.style.height = Math.min(el.input.scrollHeight, 160) + "px";
  }

  // ------------------------------------------------------------------ actions
  async function send(rawText) {
    const text = (rawText || "").trim();
    if (!text) return;
    if (text.length > state.maxChars) {
      toast(`Please keep questions under ${state.maxChars} characters.`);
      return;
    }
    if (state.activeId ? state.pending.has(state.activeId) : state.creating) return;

    el.input.value = "";
    autosize();

    // Create the thread on first message, so empty chats never reach the server.
    let id = state.activeId;
    if (!id) {
      state.creating = true;
      state.errors.delete(null);
      updateComposer();
      try {
        const created = await api("/threads", { method: "POST" });
        id = created.id;
        state.threads.unshift({ id, title: null, updatedAt: Date.now() });
        state.messages.set(id, []);
        state.loaded.add(id);
        state.activeId = id;
        saveThreads();
      } catch (_) {
        state.creating = false;
        state.errors.set(null, { text: GENERIC_ERROR, retryText: text });
        renderChat();
        return;
      } finally {
        state.creating = false;
      }
    }

    const list = state.messages.get(id) || [];
    state.messages.set(id, list);
    const userMsg = { role: "user", content: text, time: new Date() };
    list.push(userMsg);
    state.errors.delete(id);
    state.pending.add(id);
    renderThreadList();
    renderChat();

    try {
      const reply = await api(`/threads/${encodeURIComponent(id)}/messages`, {
        method: "POST", body: JSON.stringify({ content: text }),
      });
      if (reply.type === "pii_blocked") {
        // Match what the server stored, so the detail doesn't linger on screen either.
        userMsg.content = WITHHELD;
        userMsg.withheld = true;
      }
      list.push({ role: "assistant", content: reply.answer, type: reply.type,
                  citation_url: reply.citation_url, last_updated: reply.last_updated,
                  comparison: reply.comparison || null });
      state.pending.delete(id);
      const t = findThread(id);
      if (t && !t.title) {
        // The server builds the title (from the placeholder if the message was withheld).
        try {
          const full = await api(`/threads/${encodeURIComponent(id)}`);
          touchThread(id, { title: full.title });
        } catch (_) { touchThread(id, {}); }
      } else {
        touchThread(id, {});
      }
    } catch (err) {
      state.pending.delete(id);
      // A failed turn writes nothing on the server: drop the optimistic bubble too.
      const i = list.indexOf(userMsg);
      if (i >= 0) list.splice(i, 1);
      if (err.status === 404) {
        forgetThread(id);
        state.errors.set(null, { text: "That conversation no longer exists. Your question was not sent.", retryText: text });
      } else if (err.status === 422) {
        state.errors.set(id, { text: err.detail || "That message can't be sent. Please shorten or rephrase it." });
      } else if (err.status === 429) {
        state.errors.set(id, { text: "Too many messages at once — please wait a minute and try again.", retryText: text });
      } else {
        state.errors.set(id, { text: GENERIC_ERROR, retryText: text });
      }
    }
    renderThreadList();
    if (state.activeId === id || !state.activeId) renderChat();
  }

  function newChat() {
    state.activeId = null;
    state.errors.delete(null);
    saveThreads();
    renderThreadList();
    renderChat();
    closeDrawers();
    el.input.focus();
  }

  async function openThread(id) {
    closeDrawers();
    if (state.activeId === id && state.loaded.has(id)) return;
    state.activeId = id;
    saveThreads();
    renderThreadList();
    renderChat();
    if (state.pending.has(id) || state.loaded.has(id)) return;
    try {
      const thread = await api(`/threads/${encodeURIComponent(id)}`);
      state.messages.set(id, thread.messages.map(fromServer));
      state.loaded.add(id);
      const t = findThread(id);
      if (t && thread.title && t.title !== thread.title) { t.title = thread.title; saveThreads(); }
    } catch (err) {
      if (err.status === 404) {
        forgetThread(id);
        toast("That conversation was removed from the server.");
      } else {
        state.errors.set(id, { text: GENERIC_ERROR });
      }
    }
    renderThreadList();
    if (state.activeId === id || !state.activeId) renderChat();
  }

  async function deleteThread(id) {
    const t = findThread(id);
    if (!t) return;
    if (!window.confirm(`Delete "${threadTitle(t)}"? This removes the conversation from the server.`)) return;
    try {
      await api(`/threads/${encodeURIComponent(id)}`, { method: "DELETE" });
    } catch (err) {
      if (err.status !== 404) { toast(GENERIC_ERROR); return; }
    }
    const wasActive = state.activeId === id;
    forgetThread(id);
    renderThreadList();
    if (wasActive) renderChat();
    toast("Conversation deleted.");
  }

  // ------------------------------------------------------------------ pin, rename, export
  /** Pins are a per-browser way of organising chats, so they live with the thread list. */
  function togglePin(id) {
    const t = findThread(id);
    if (!t) return;
    t.pinned = !t.pinned;
    saveThreads();
    renderThreadList();
    renderHeader();
    toast(t.pinned ? "Chat pinned." : "Chat unpinned.");
  }

  /** An input that commits on Enter or blur and cancels on Escape. */
  function inlineEditor(current, onDone) {
    const input = h("input", { class: "rename-input", type: "text", maxlength: "60",
                               "aria-label": "Chat name", autocomplete: "off" });
    input.value = current;
    let done = false;
    const finish = (save) => {
      if (done) return;
      done = true;
      onDone(save ? input.value : null);
    };
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); finish(true); }
      else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); finish(false); }
    });
    input.addEventListener("blur", () => finish(true));
    return input;
  }

  /** Rename in place: in the sidebar row when it's on screen, otherwise in the chat header. */
  async function startRename(id, where) {
    const t = findThread(id);
    if (!t) return;
    const current = t.title && !t.title.startsWith("[message withheld") ? t.title : "";
    const li = el.threadList.querySelector(`[data-thread="${CSS.escape(id)}"]`);
    if (where === "sidebar" && li && li.offsetParent !== null) {
      const input = inlineEditor(current, async (value) => {
        await renameThread(id, value);
        renderThreadList();
        const again = el.threadList.querySelector(`[data-thread="${CSS.escape(id)}"] .thread-item__open`);
        if (again) again.focus();
      });
      li.replaceChildren(input);
      input.focus();
      input.select();
      return;
    }
    if (state.activeId !== id) await openThread(id);
    closeDrawers(false);
    const input = inlineEditor(current, async (value) => {
      input.remove();
      el.chatTitle.hidden = false;
      el.renameCurrent.hidden = false;
      await renameThread(id, value);
      renderHeader();
      el.renameCurrent.focus();
    });
    el.chatTitle.hidden = true;
    el.renameCurrent.hidden = true;
    el.chatHeading.prepend(input);
    input.focus();
    input.select();
  }

  async function renameThread(id, value) {
    const t = findThread(id);
    const title = (value || "").replace(/\s+/g, " ").trim();
    if (!t || value == null || !title || title === t.title) return;
    try {
      const res = await api(`/threads/${encodeURIComponent(id)}`, {
        method: "PATCH", body: JSON.stringify({ title }),
      });
      // The server's title (PII-checked, length-limited) is what we keep; activity time stays.
      t.title = res.title;
      saveThreads();
      renderThreadList();
      renderHeader();
      toast("Chat renamed.");
    } catch (err) {
      if (err.status === 404) {
        forgetThread(id);
        renderThreadList();
        renderChat();
        toast("That conversation no longer exists.");
      } else if (err.status === 422) {
        toast(err.detail || "That name can't be used.");
      } else {
        toast(GENERIC_ERROR);
      }
    }
  }

  function fromServer(m) {
    return {
      role: m.role, content: m.content, type: m.type, citation_url: m.citation_url,
      last_updated: m.last_updated, comparison: m.comparison || null,
      time: m.role === "user" ? new Date(m.created_at) : null,
      withheld: m.role === "user" && m.content === WITHHELD, shown: true,
    };
  }

  function mdEscape(text) {
    return String(text).replace(/([\\`*_[\]<>#|~])/g, "\\$1");
  }

  function slug(text) {
    return String(text).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 50);
  }

  function toMarkdown(t, msgs) {
    const now = new Date();
    const today = `${String(now.getDate()).padStart(2, "0")} ${MONTHS[now.getMonth()]} ${now.getFullYear()}`;
    const disclaimer = (state.meta && state.meta.disclaimer) || "Facts-only. No investment advice.";
    const lines = [
      `# ${mdEscape(threadTitle(t))}`, "",
      `*${disclaimer}*`, "",
      `Exported from the Mutual Fund FAQ Assistant on ${today}, ${formatTime(now)}.`, "",
    ];
    for (const m of msgs) {
      lines.push("---", "");
      if (m.role === "user") {
        const when = m.time && !isNaN(m.time) ? ` (${formatTime(m.time)})` : "";
        lines.push(`### You${when}`, "", mdEscape(m.content), "");
        continue;
      }
      const kind = (KINDS[m.type] || KINDS.answer).label;
      lines.push(`### Assistant: ${kind}`, "", mdEscape(m.content), "");
      const table = m.comparison;
      if (table && Array.isArray(table.columns) && table.columns.length) {
        const cell = (v) => mdEscape(String(v).replace(/\s+/g, " "));
        lines.push(`| Field | ${table.columns.map((c) => cell(c.name)).join(" | ")} |`,
                   `|${"---|".repeat(table.columns.length + 1)}`,
                   ...(table.rows || []).map((r) => `| ${[r.label, ...r.cells].map(cell).join(" | ")} |`), "");
        for (const note of table.notes || []) lines.push(mdEscape(note), "");
        lines.push("Sources:", "");
        for (const c of table.columns) {
          const u = safeUrl(c.url);
          if (u) lines.push(`- ${mdEscape(c.name)}: <${u.href}>`);
        }
        lines.push("");
        const when = formatDate(m.last_updated);
        if (when) lines.push(`Last updated from sources: ${when}`);
        lines.push("");
        continue;
      }
      const url = safeUrl(m.citation_url);
      if (url) lines.push(`Source: <${url.href}>  `);
      const updated = formatDate(m.last_updated);
      if (updated) lines.push(`Last updated from sources: ${updated}`);
      lines.push("");
    }
    return lines.join("\n");
  }

  async function exportThread(id) {
    const t = findThread(id);
    if (!t) return;
    let msgs = state.messages.get(id);
    if (!state.loaded.has(id) || !msgs) {
      try {
        const thread = await api(`/threads/${encodeURIComponent(id)}`);
        msgs = thread.messages.map(fromServer);
        state.messages.set(id, msgs);
        state.loaded.add(id);
      } catch (err) {
        toast(err.status === 404 ? "That conversation no longer exists." : GENERIC_ERROR);
        return;
      }
    }
    if (!msgs.length) { toast("This chat has no messages to export yet."); return; }
    const blob = new Blob([toMarkdown(t, msgs)], { type: "text/markdown;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = h("a", { href: url, download: `${slug(threadTitle(t)) || "chat"}.md`, hidden: true });
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast("Chat exported as Markdown.");
  }

  // ------------------------------------------------------------------ thread action menu
  let menuFor = null;
  let menuButton = null;

  function openMenu(id, button) {
    if (menuFor === id) { closeMenu(); return; }
    closeMenu(false);
    const t = findThread(id);
    if (!t) return;
    const item = (label, name, action, cls) => h("button", {
      class: "menu__item" + (cls ? " " + cls : ""), type: "button", role: "menuitem", tabindex: "-1",
      onclick: () => { closeMenu(false); action(); },
    }, icon(name), label);
    el.menu.replaceChildren(
      item(t.pinned ? "Unpin" : "Pin", "pin", () => togglePin(id)),
      item("Rename", "edit", () => startRename(id, "sidebar")),
      item("Export as Markdown (.md)", "download", () => exportThread(id)),
      h("div", { class: "menu__sep", role: "separator" }),
      item("Delete", "trash", () => deleteThread(id), "menu__item--danger"));
    el.menu.hidden = false;
    const r = button.getBoundingClientRect();
    const w = el.menu.offsetWidth, ht = el.menu.offsetHeight;
    const left = Math.max(8, Math.min(r.left, window.innerWidth - w - 8));
    const top = r.bottom + 4 + ht > window.innerHeight - 8 ? Math.max(8, r.top - ht - 4) : r.bottom + 4;
    el.menu.style.left = left + "px";
    el.menu.style.top = top + "px";
    button.setAttribute("aria-expanded", "true");
    menuFor = id;
    menuButton = button;
    el.menu.querySelector(".menu__item").focus();
  }

  function closeMenu(restoreFocus = true) {
    if (menuFor === null) return;
    el.menu.hidden = true;
    el.menu.replaceChildren();
    if (menuButton) {
      menuButton.setAttribute("aria-expanded", "false");
      if (restoreFocus && menuButton.isConnected) menuButton.focus();
    }
    menuFor = null;
    menuButton = null;
  }

  function menuKeys(e) {
    const items = [...el.menu.querySelectorAll(".menu__item")];
    const i = items.indexOf(document.activeElement);
    if (e.key === "ArrowDown") { e.preventDefault(); items[(i + 1) % items.length].focus(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); items[(i - 1 + items.length) % items.length].focus(); }
    else if (e.key === "Home") { e.preventDefault(); items[0].focus(); }
    else if (e.key === "End") { e.preventDefault(); items[items.length - 1].focus(); }
    else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closeMenu(); }
    else if (e.key === "Tab") closeMenu(false);
  }

  // ------------------------------------------------------------------ collapsible sidebar
  const COLLAPSE_KEY = "mfqa.sidebar.collapsed.v1";

  function setCollapsed(collapsed, persist = true) {
    state.collapsed = collapsed;
    el.app.classList.toggle("is-collapsed", collapsed);
    const label = collapsed ? "Expand sidebar" : "Collapse sidebar";
    el.collapse.setAttribute("aria-expanded", String(!collapsed));
    el.collapse.setAttribute("aria-label", label);
    el.collapse.title = `${label} (Ctrl+B)`;
    if (persist) storage.set(COLLAPSE_KEY, collapsed);
  }

  // ------------------------------------------------------------------ scheme panel
  function schemeGroup(s) {
    const head = (s.category || "").split(" - ")[0].trim();
    return head === "FoF" ? "Other" : head || "Other";
  }

  /** Every source page an assistant reply cites. A comparison cites one page per column; its
   *  citation_url is only the first column's, so the columns are read too. */
  function citedSourceUrls(msgs) {
    const urls = new Set();
    for (const m of msgs) {
      if (m.role !== "assistant") continue;
      if (m.citation_url) urls.add(m.citation_url);
      const columns = m.comparison && Array.isArray(m.comparison.columns) ? m.comparison.columns : [];
      for (const c of columns) if (c && c.url) urls.add(c.url);
    }
    return urls;
  }

  function citedUrls() {
    return citedSourceUrls(state.activeId ? state.messages.get(state.activeId) || [] : []);
  }

  function renderScopeChips() {
    const schemes = (state.meta && state.meta.schemes) || [];
    const groups = ["All", ...new Set(schemes.map(schemeGroup))];
    el.scopeChips.replaceChildren(...groups.map((g) => h("button", {
      class: "chip", type: "button", "aria-pressed": String(g === state.scopeGroup),
      onclick: () => { state.scopeGroup = g; renderScopeChips(); renderSchemes(); },
    }, g)));
  }

  function renderSchemes() {
    const schemes = (state.meta && state.meta.schemes) || [];
    el.scopeCount.textContent = `${schemes.length} schemes`;
    const q = state.scopeFilter.trim().toLowerCase();
    const cited = citedUrls();
    const shown = schemes.filter((s) =>
      (state.scopeGroup === "All" || schemeGroup(s) === state.scopeGroup) &&
      (!q || `${s.name} ${s.amc} ${s.category}`.toLowerCase().includes(q)));
    if (!shown.length) {
      el.schemeList.replaceChildren(h("li", { class: "scope__empty",
        text: schemes.length ? "No schemes match." : "Scheme list unavailable." }));
      return;
    }
    // Schemes cited in this conversation first, then alphabetical.
    shown.sort((a, b) => (cited.has(b.url) - cited.has(a.url)) || a.name.localeCompare(b.name));
    el.schemeList.replaceChildren(...shown.map((s) => {
      const url = safeUrl(s.url);
      const isCited = cited.has(s.url);
      return h("li", null, h(url ? "a" : "div", {
        class: "scheme" + (isCited ? " scheme--active" : ""),
        href: url ? url.href : null, target: url ? "_blank" : null, rel: url ? "noopener noreferrer" : null,
        title: url ? `Open the source page for ${s.name} (new tab)` : null,
      },
        h("span", { class: "scheme__name" }, h("span", { text: s.name }), url ? icon("external") : null),
        h("span", { class: "scheme__meta" },
          h("span", { text: s.amc }),
          h("span", { class: "scheme__cat", text: s.category.replace(/^Equity - /, "") })),
        isCited ? h("span", { class: "scheme__cited" }, icon("shield"), "Cited in this chat") : null));
    }));
  }

  // ------------------------------------------------------------------ status card
  function renderStatus(health, failed) {
    el.statusDot.className = "status-dot";
    if (failed) {
      el.statusDot.classList.add("status-dot--warn");
      el.statusTitle.textContent = "Server unreachable";
      el.statusBody.textContent = "Can't reach the assistant right now. Check that the backend is running.";
      return;
    }
    const updated = formatDate(health.last_ingestion);
    if (!health.index_loaded) {
      el.statusDot.classList.add("status-dot--warn");
      el.statusTitle.textContent = "Search index missing";
      el.statusBody.textContent = "Run the ingestion step on the server to index the sources.";
    } else if (!health.llm_configured) {
      el.statusDot.classList.add("status-dot--warn");
      el.statusTitle.textContent = "Limited mode";
      el.statusBody.textContent = "The language model isn't configured, so answers use fixed fact templates.";
    } else if (health.stale_sources && health.stale_sources.length) {
      const n = health.stale_sources.length;
      el.statusDot.classList.add("status-dot--warn");
      el.statusTitle.textContent = "Some sources are out of date";
      el.statusBody.textContent = `${n} source${n === 1 ? " hasn't" : "s haven't"} been refreshed for over ${health.stale_after_days} days. Each answer shows when its source was last updated.`;
    } else {
      el.statusDot.classList.add("status-dot--ok");
      el.statusTitle.textContent = "Sources indexed";
      el.statusBody.textContent = updated
        ? `Answers come only from the indexed sources, last updated ${updated}.`
        : "Answers come only from the indexed sources.";
    }
  }

  // ------------------------------------------------------------------ drawers
  let lastToggle = null;
  function openDrawer(drawer, toggle) {
    closeDrawers(false);
    drawer.classList.add("is-open");
    el.scrim.hidden = false;
    toggle.setAttribute("aria-expanded", "true");
    lastToggle = toggle;
    const first = drawer.querySelector("button, input, a[href]");
    if (first) first.focus();
  }
  function closeDrawers(restoreFocus = true) {
    const wasOpen = el.sidebar.classList.contains("is-open") || el.scope.classList.contains("is-open");
    el.sidebar.classList.remove("is-open");
    el.scope.classList.remove("is-open");
    el.scrim.hidden = true;
    el.openSidebar.setAttribute("aria-expanded", "false");
    el.openScope.setAttribute("aria-expanded", "false");
    if (wasOpen && restoreFocus && lastToggle && lastToggle.offsetParent !== null) lastToggle.focus();
  }

  // ------------------------------------------------------------------ wiring
  function bind() {
    el.composer.addEventListener("submit", (e) => { e.preventDefault(); send(el.input.value); });
    el.input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        if (!el.send.disabled) send(el.input.value);
      }
    });
    el.input.addEventListener("input", () => { autosize(); updateComposer(); });
    el.newChat.addEventListener("click", newChat);
    el.deleteCurrent.addEventListener("click", () => state.activeId && deleteThread(state.activeId));
    el.renameCurrent.addEventListener("click", () => state.activeId && startRename(state.activeId, "header"));
    el.chatTitle.addEventListener("dblclick", () => state.activeId && startRename(state.activeId, "header"));
    el.pinCurrent.addEventListener("click", () => state.activeId && togglePin(state.activeId));
    el.exportCurrent.addEventListener("click", () => state.activeId && exportThread(state.activeId));
    el.collapse.addEventListener("click", () => setCollapsed(!state.collapsed));
    el.menu.addEventListener("keydown", menuKeys);
    document.addEventListener("pointerdown", (e) => {
      if (menuFor !== null && !el.menu.contains(e.target) && !(menuButton && menuButton.contains(e.target))) {
        closeMenu(false);
      }
    });
    window.addEventListener("resize", () => closeMenu(false));
    el.threadList.addEventListener("scroll", () => closeMenu(false));
    document.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase() === "b" && !narrow.matches) {
        e.preventDefault();
        setCollapsed(!state.collapsed);
      }
    });
    el.threadSearch.addEventListener("input", () => { state.search = el.threadSearch.value; renderThreadList(); });
    el.scopeFilter.addEventListener("input", () => { state.scopeFilter = el.scopeFilter.value; renderSchemes(); });
    el.openSidebar.addEventListener("click", () => openDrawer(el.sidebar, el.openSidebar));
    el.openScope.addEventListener("click", () => openDrawer(el.scope, el.openScope));
    el.scrim.addEventListener("click", () => closeDrawers());
    document.querySelectorAll("[data-close-drawer]").forEach((b) => b.addEventListener("click", () => closeDrawers()));
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawers(); });
    // Another tab may have created or deleted threads.
    window.addEventListener("storage", (e) => {
      if (e.key !== STORE_KEY) return;
      loadThreads();
      if (state.activeId && !findThread(state.activeId)) state.activeId = null;
      renderThreadList();
      renderChat({ scroll: false });
    });
  }

  async function init() {
    bind();
    setCollapsed(storage.get(COLLAPSE_KEY, false) === true, false);
    updatePlaceholder();
    if (narrow.addEventListener) narrow.addEventListener("change", updatePlaceholder);
    loadThreads();
    const saved = storage.get(ACTIVE_KEY, null);
    renderWelcome();
    renderThreadList();

    const [meta, health] = await Promise.allSettled([api("/meta"), api("/health")]);
    if (meta.status === "fulfilled") {
      state.meta = meta.value;
      state.maxChars = meta.value.max_input_chars || 500;
      el.input.maxLength = state.maxChars;
    }
    renderStatus(health.status === "fulfilled" ? health.value : null, health.status !== "fulfilled");
    renderWelcome();
    renderScopeChips();

    if (saved && findThread(saved)) await openThread(saved);
    else renderChat();
  }

  init();
})();
