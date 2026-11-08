/* CIRQ comment panel
 * One shared panel for reels and posts. It never loads a page:
 *   - wide screens, reels page  -> docks right beside the reel ("dock")
 *   - wide screens, other pages -> floats at the right edge ("side")
 *   - phones / narrow windows   -> slides up as a bottom sheet ("sheet")
 * Comments load 20 at a time (infinite scroll), new comments appear instantly (optimistic) and the
 * server stays the source of truth for permissions, moderation and rate limits.
 */
(() => {
  "use strict";
  const panel = document.querySelector("[data-cpanel]");
  if (!panel || !window.CIRQ) return;

  const $ = (sel, root = panel) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
  const { api, toast } = window.CIRQ;

  const backdrop = document.querySelector(".cpanel-backdrop");
  const body = $("[data-cpanel-body]"), list = $("[data-cpanel-list]"), stateBox = $("[data-cpanel-state]");
  const sentinel = $("[data-cpanel-sentinel]"), countEl = $("[data-cpanel-count]");
  const form = $("[data-cpanel-form]"), input = $("[data-cpanel-input]"), sendBtn = $("[data-cpanel-send]"), chars = $("[data-cpanel-chars]");
  const me = $("[data-cpanel-me]"), grip = $("[data-cpanel-grip]"), head = $("[data-cpanel-head]");
  const MAX = 500, DOCK_MIN = 300, DOCK_MAX = 360;
  const sheetMQ = window.matchMedia("(max-width: 699px)");
  const finePointer = window.matchMedia("(pointer: fine)");
  const ICON = (n) => `<svg class="i" aria-hidden="true"><use href="#i-${n}"/></svg>`;

  const S = { open: false, id: null, trigger: null, cursor: "", hasMore: false, loading: false, sending: false, token: 0 };

  /* ---------- helpers ---------- */
  const fmt = (n) => {
    n = Math.max(0, Number(n) || 0);
    if (n >= 1e6) return `${(n / 1e6).toFixed(1).replace(/\.0$/, "")}M`;
    if (n >= 1e4) return `${Math.round(n / 1e3)}K`;
    if (n >= 1e3) return `${(n / 1e3).toFixed(1).replace(/\.0$/, "")}K`;
    return String(n);
  };
  function nodesFrom(html) {
    const t = document.createElement("template");
    t.innerHTML = html.trim();
    return [...t.content.children];
  }
  const hasRows = () => !!$(".crow", list);
  function friendly(err) {
    if (err && err.status === 429) return "You're doing that too fast. Try again in a moment.";
    return (err && err.message) || "Something went wrong.";
  }

  function setCount(id, n) {
    $$(`[data-comment-count-for="${CSS.escape(String(id))}"]`).forEach((el) => (el.textContent = fmt(n)));
    if (S.id === id) countEl.textContent = n > 0 ? n.toLocaleString() : "";
  }
  function bumpCount(id, delta) {
    const first = $$(`[data-comment-count-for="${CSS.escape(String(id))}"]`)[0];
    const raw = first ? first.textContent.trim() : "0";
    // Counts are abbreviated (1.2K) on the page, so only do local maths for plain numbers; the server value wins afterwards.
    if (/^\d+$/.test(raw)) setCount(id, Math.max(0, Number(raw) + delta));
  }

  /* ---------- layout mode ---------- */
  function applyMode() {
    const host = document.querySelector("[data-comments-host]");
    let mode = "side", width = DOCK_MAX;
    if (sheetMQ.matches) mode = "sheet";
    else if (host) {
      const avail = window.innerWidth - 8 - (host.getBoundingClientRect().right + 12);
      if (avail >= DOCK_MIN) { mode = "dock"; width = Math.min(DOCK_MAX, avail); } else mode = "sheet";
    }
    const parent = mode === "dock" ? host : document.body;
    if (panel.parentNode !== parent) parent.appendChild(panel);
    panel.dataset.mode = mode;
    panel.style.setProperty("--cp-w", `${Math.round(width)}px`);
    panel.setAttribute("aria-modal", mode === "sheet" ? "true" : "false");
    const sheetOpen = S.open && mode === "sheet";
    backdrop.hidden = !sheetOpen;
    document.documentElement.classList.toggle("cpanel-lock", sheetOpen);
  }

  /* ---------- open / close ---------- */
  function markTriggers() {
    $$("[data-comments]").forEach((t) => t.setAttribute("aria-expanded", S.open && t.dataset.comments === S.id ? "true" : "false"));
  }
  function resetList() {
    S.token++; S.cursor = ""; S.hasMore = false; S.loading = false;
    list.innerHTML = ""; stateBox.innerHTML = ""; body.scrollTop = 0;
  }
  function openFor(id, trigger) {
    S.id = id; S.trigger = trigger || S.trigger;
    resetList();
    countEl.textContent = "";
    const first = $$(`[data-comment-count-for="${CSS.escape(String(id))}"]`)[0];
    if (first && /^\d+$/.test(first.textContent.trim()) && Number(first.textContent) > 0) countEl.textContent = Number(first.textContent).toLocaleString();
    if (!S.open) {
      S.open = true;
      applyMode();
      panel.inert = false;
      panel.setAttribute("aria-hidden", "false");
      requestAnimationFrame(() => { panel.classList.add("open"); if (panel.dataset.mode === "sheet") backdrop.classList.add("show"); });
      setTimeout(() => panel.focus({ preventScroll: true }), 60);
    }
    markTriggers();
    fetchPage();
  }
  function close(restoreFocus = true) {
    if (!S.open) return;
    S.open = false; S.token++;
    panel.classList.remove("open"); backdrop.classList.remove("show");
    panel.setAttribute("aria-hidden", "true");
    panel.inert = true;
    document.documentElement.classList.remove("cpanel-lock");
    setTimeout(() => { if (!S.open) { backdrop.hidden = true; list.innerHTML = ""; stateBox.innerHTML = ""; } }, 320);
    markTriggers();
    if (restoreFocus && S.trigger && document.contains(S.trigger)) S.trigger.focus({ preventScroll: true });
  }

  document.addEventListener("click", (e) => {
    const t = e.target.closest("[data-comments]");
    if (t) {
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.button === 1) return;   // let people open the full page in a new tab
      if (document.getElementById("comments")) return;                       // already on the post page: use its inline list
      e.preventDefault();
      if (S.open && S.id === t.dataset.comments) close(); else openFor(t.dataset.comments, t);
      return;
    }
    if (e.target.closest("[data-cpanel-close]")) { close(); return; }
    if (e.target.closest("[data-cpanel-retry]")) { fetchPage(); return; }
    const del = e.target.closest("[data-comment-delete]");
    if (del && panel.contains(del)) { removeComment(del); return; }
    const em = e.target.closest("[data-cemoji]");
    if (em && panel.contains(em)) { insertEmoji(em.dataset.cemoji); }
  });

  document.addEventListener("keydown", (e) => {
    if (!S.open) return;
    if (e.key === "Escape" && !document.querySelector("dialog[open]")) { e.preventDefault(); close(); return; }
    if (e.key === "Tab" && panel.dataset.mode === "sheet") {              // keep focus inside the modal sheet
      const f = $$('a[href], button:not([disabled]), textarea, [tabindex]:not([tabindex="-1"])', panel).filter((x) => x.offsetParent !== null);
      if (!f.length) return;
      const first = f[0], last = f[f.length - 1];
      if (e.shiftKey && (document.activeElement === first || document.activeElement === panel)) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  });

  // While a reel is open the panel follows whichever reel is on screen.
  document.addEventListener("cirq:reel-active", (e) => {
    const id = e.detail && e.detail.id;
    if (S.open && id && id !== S.id && document.querySelector("[data-comments-host]")) openFor(id);
  });
  let resizeT = 0;
  window.addEventListener("resize", () => { clearTimeout(resizeT); resizeT = setTimeout(() => { if (S.open) applyMode(); }, 120); });

  /* ---------- loading comments (cursor pagination + infinite scroll) ---------- */
  function renderState(kind, msg) {
    if (kind === "loading") {
      stateBox.innerHTML = hasRows()
        ? `<div class="cpanel-more" role="status">${ICON("loader")}</div>`
        : Array.from({ length: 5 }, () => '<div class="cskel" aria-hidden="true"><i class="a"></i><div><i class="l1"></i><i class="l2"></i></div></div>').join("");
    } else if (kind === "empty") {
      stateBox.innerHTML = `<div class="cpanel-empty">${ICON("comment")}<h3>No comments yet</h3><p>Start the conversation.</p></div>`;
    } else if (kind === "error") {
      stateBox.innerHTML = `<div class="cpanel-empty" role="alert"><p></p><button class="btn sm ghost" type="button" data-cpanel-retry>Try again</button></div>`;
      $("p", stateBox).textContent = msg || "Couldn't load comments.";
    } else stateBox.innerHTML = "";
  }
  async function fetchPage() {
    if (S.loading || !S.id) return;
    const token = S.token, id = S.id;
    S.loading = true; renderState("loading");
    try {
      const url = `/api/posts/${encodeURIComponent(id)}/comments${S.cursor ? `?before=${encodeURIComponent(S.cursor)}` : ""}`;
      const r = await api(url, { method: "GET" });
      if (token !== S.token) return;
      const have = new Set($$(".crow[data-comment]", list).map((n) => n.dataset.comment));
      nodesFrom(r.html).forEach((n) => { if (!have.has(n.dataset.comment)) list.append(n); });
      S.cursor = r.next_cursor || ""; S.hasMore = !!r.has_more;
      setCount(id, r.count);
      renderState(hasRows() ? "none" : (S.hasMore ? "none" : "empty"));
      S.loading = false;
      if (S.hasMore) requestAnimationFrame(checkSentinel);               // fill the panel if the first page is short
    } catch (err) {
      if (token !== S.token) return;
      S.loading = false;
      renderState("error", friendly(err));
    }
  }
  const io = "IntersectionObserver" in window ? new IntersectionObserver((entries) => {
    if (S.open && S.hasMore && !S.loading && entries.some((en) => en.isIntersecting)) fetchPage();
  }, { root: body, rootMargin: "240px 0px" }) : null;
  if (io) io.observe(sentinel);
  function checkSentinel() {
    const b = body.getBoundingClientRect(), s = sentinel.getBoundingClientRect();
    if (S.open && S.hasMore && !S.loading && s.top < b.bottom + 240) fetchPage();
  }
  if (!io) body.addEventListener("scroll", checkSentinel, { passive: true });

  /* ---------- posting (optimistic) ---------- */
  function autosize() {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 112)}px`;
  }
  function syncForm() {
    const n = input.value.length, has = input.value.trim().length > 0;
    sendBtn.disabled = !has || S.sending;
    chars.hidden = n < MAX - 100;
    chars.textContent = `${n}/${MAX}`;
    chars.classList.toggle("warn", n >= MAX - 20);
  }
  input.addEventListener("input", () => { autosize(); syncForm(); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing && finePointer.matches) { e.preventDefault(); if (!sendBtn.disabled) form.requestSubmit(); }
  });
  function insertEmoji(emoji) {
    if (input.value.length + emoji.length > MAX) return;
    const a = input.selectionStart == null ? input.value.length : input.selectionStart;
    const b = input.selectionEnd == null ? a : input.selectionEnd;
    input.setRangeText(emoji, a, b, "end");
    input.dispatchEvent(new Event("input"));
    input.focus({ preventScroll: true });
  }
  function pendingRow(text) {
    const el = document.createElement("article");
    el.className = "crow pending";
    el.innerHTML = `<span class="crow-av">${me.innerHTML}</span><div class="crow-main"><div class="crow-top"><span class="crow-name"></span><time class="crow-time">Posting…</time></div><p class="crow-text"></p></div>`;
    $(".crow-name", el).textContent = me.dataset.username || "you";
    $(".crow-text", el).textContent = text;                              // text only, never HTML
    return el;
  }
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text || S.sending || !S.id) return;
    const id = S.id, row = pendingRow(text);
    S.sending = true;
    input.value = ""; autosize(); syncForm();
    stateBox.innerHTML = "";
    list.prepend(row);
    body.scrollTo({ top: 0, behavior: "smooth" });
    try {
      const fd = new FormData();
      fd.set("body", text); fd.set("view", "panel");
      const r = await api(`/posts/${encodeURIComponent(id)}/comments`, { body: fd });
      setCount(id, r.count);
      if (S.id === id) {
        const [node] = nodesFrom(r.html);
        if (node) { node.classList.add("fresh"); row.replaceWith(node); } else row.remove();
      }
    } catch (err) {
      row.remove();
      if (S.id === id) { input.value = text; autosize(); if (!hasRows() && !S.hasMore) renderState("empty"); }
      toast(friendly(err), "error");
    } finally {
      S.sending = false; syncForm();
      if (finePointer.matches && S.open) input.focus({ preventScroll: true });
    }
  });

  /* ---------- deleting ---------- */
  async function removeComment(btn) {
    const id = btn.dataset.commentDelete, row = btn.closest(".crow");
    if (!id || !row || row.classList.contains("removing")) return;
    if (!window.confirm("Delete this comment?")) return;
    row.classList.add("removing");
    try {
      const r = await api(`/comments/${encodeURIComponent(id)}/delete`);
      row.classList.add("gone");
      setTimeout(() => { row.remove(); if (!hasRows() && !S.hasMore && S.open) renderState("empty"); }, 220);
      if (typeof r.count === "number") setCount(S.id, r.count); else bumpCount(S.id, -1);
    } catch (err) {
      row.classList.remove("removing");
      toast(friendly(err), "error");
    }
  }

  /* ---------- swipe the sheet down to dismiss ---------- */
  let drag = null;
  [grip, head].forEach((el) => {
    el.addEventListener("pointerdown", (e) => {
      if (panel.dataset.mode !== "sheet" || e.target.closest("button")) return;
      drag = { y: e.clientY, dy: 0, id: e.pointerId, el };
      panel.classList.add("dragging");
      try { el.setPointerCapture(e.pointerId); } catch (_) { /* older browsers */ }
    });
    el.addEventListener("pointermove", (e) => {
      if (!drag || e.pointerId !== drag.id) return;
      drag.dy = Math.max(0, e.clientY - drag.y);
      panel.style.transform = `translateY(${drag.dy}px)`;
    });
    const end = (e) => {
      if (!drag || e.pointerId !== drag.id) return;
      const dy = drag.dy; drag = null;
      panel.classList.remove("dragging"); panel.style.transform = "";
      if (dy > 110) close();
    };
    el.addEventListener("pointerup", end);
    el.addEventListener("pointercancel", end);
  });

  syncForm();
})();
