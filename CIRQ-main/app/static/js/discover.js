/* CIRQ Discover > Accounts: live search, category chips, "show more" and the trending rail, all without a page load.
 * Every control is also a plain link/form, so the page keeps working if this script fails to load. */
(() => {
  "use strict";
  const body = document.querySelector("[data-dsc-body]");
  const form = document.querySelector("[data-dsc-form]");
  if (!body || !form) return;
  const input = form.querySelector("[data-dsc-input]"), clear = form.querySelector("[data-dsc-clear]");
  const KEYS = ["q", "cat", "school", "view"];
  const base = body.dataset.url || "/discover";
  let state = fromUrl(location.search), ctrl = null, token = 0, typing = 0;

  function fromUrl(search) {
    const p = new URLSearchParams(search), s = {};
    KEYS.forEach((k) => { if (p.get(k)) s[k] = p.get(k); });
    return s;
  }
  function toUrl(s, extra = {}) {
    const u = new URL(base, location.origin);
    u.searchParams.set("tab", "people");
    Object.entries({ ...s, ...extra }).forEach(([k, v]) => { if (v) u.searchParams.set(k, v); });
    return u;
  }
  const skeleton = () => `<div class="dsc-section"><div class="dsc-grid" aria-hidden="true">${
    Array.from({ length: 8 }, () => '<div class="dsc-skel-card"><i class="c"></i><i class="l"></i><i class="l s"></i><i class="b"></i></div>').join("")}</div></div>`;
  const syncInput = () => { input.value = state.q || ""; clear.hidden = !input.value; };

  async function load({ push = false } = {}) {
    const mine = ++token;
    if (ctrl) ctrl.abort();
    ctrl = new AbortController();
    const page = toUrl(state), fetchUrl = toUrl(state, { partial: "1" });
    body.classList.add("is-busy"); body.setAttribute("aria-busy", "true");
    const slow = setTimeout(() => { if (mine === token) { body.innerHTML = skeleton(); body.classList.remove("is-busy"); } }, 180);
    try {
      const res = await fetch(fetchUrl, { credentials: "same-origin", signal: ctrl.signal, headers: { "X-Requested-With": "fetch" } });
      if (!res.ok) throw new Error(`Request failed (${res.status})`);
      const html = await res.text();
      if (mine !== token) return;
      body.innerHTML = html;
      history[push ? "pushState" : "replaceState"](null, "", page.pathname + page.search);
    } catch (err) {
      if (err.name === "AbortError" || mine !== token) return;
      body.innerHTML = '<div class="dsc-error" role="alert"><p>Couldn\'t load accounts. Check your connection and try again.</p><button class="btn sm" type="button" data-dsc-retry>Try again</button></div>';
    } finally {
      clearTimeout(slow);
      if (mine === token) { body.classList.remove("is-busy"); body.removeAttribute("aria-busy"); }
    }
  }

  /* search: as you type (debounced), on Enter, and the clear button */
  input.addEventListener("input", () => {
    clear.hidden = !input.value;
    clearTimeout(typing);
    typing = setTimeout(() => {
      const q = input.value.trim();
      if ((state.q || "") === q) return;
      state = { ...state, q: q || undefined };
      delete state.view;
      if (!state.q) delete state.q;
      load();
    }, 260);
  });
  form.addEventListener("submit", (e) => {
    e.preventDefault(); clearTimeout(typing);
    const q = input.value.trim();
    state = { ...state }; delete state.view; if (q) state.q = q; else delete state.q;
    load({ push: true });
  });
  clear.addEventListener("click", () => {
    clearTimeout(typing); delete state.q; syncInput(); input.focus(); load({ push: true });
  });
  input.addEventListener("keydown", (e) => { if (e.key === "Escape" && input.value) { e.preventDefault(); clear.click(); } });

  /* chips, "See all", clear links (delegated, because the body is replaced) */
  body.addEventListener("click", (e) => {
    const a = e.target.closest("a[data-dsc-nav]");
    if (a && !(e.metaKey || e.ctrlKey || e.shiftKey || e.button === 1)) {
      e.preventDefault();
      state = fromUrl(new URL(a.href).search);
      syncInput(); load({ push: true });
      return;
    }
    if (e.target.closest("[data-dsc-retry]")) { load(); return; }
    const sc = e.target.closest("[data-dsc-scroll]");
    if (sc) {
      const rail = body.querySelector("[data-dsc-rail]");
      if (rail) rail.scrollBy({ left: Number(sc.dataset.dscScroll) * rail.clientWidth * 0.85, behavior: "smooth" });
      return;
    }
    const more = e.target.closest("[data-dsc-more]");
    if (more) showMore(more);
  });
  body.addEventListener("change", (e) => {
    if (!e.target.matches("[data-dsc-school]")) return;
    state = { ...state }; delete state.view;
    if (e.target.value) state.school = e.target.value; else delete state.school;
    delete state.cat;
    load({ push: true });
  });
  window.addEventListener("popstate", () => { state = fromUrl(location.search); syncInput(); load(); });

  async function showMore(btn) {
    const grid = body.querySelector("[data-dsc-results]");
    if (!grid || btn.disabled) return;
    btn.disabled = true;
    const label = btn.textContent; btn.textContent = "Loading…";
    try {
      const res = await fetch(toUrl(state, { more: "1", page: btn.dataset.next }), { credentials: "same-origin", headers: { "X-Requested-With": "fetch" } });
      if (!res.ok) throw new Error();
      const t = document.createElement("template");
      t.innerHTML = (await res.text()).trim();
      const have = new Set([...grid.querySelectorAll("[data-dsc-card]")].map((c) => c.dataset.username));
      grid.querySelectorAll("[data-dsc-next]").forEach((n) => n.remove());
      [...t.content.querySelectorAll("[data-dsc-card]")].forEach((c, i) => { if (!have.has(c.dataset.username)) { c.style.setProperty("--i", i); grid.append(c); } });
      const next = t.content.querySelector("[data-dsc-next]");
      if (next) { btn.dataset.next = next.dataset.dscNext; btn.disabled = false; btn.textContent = label; }
      else btn.closest(".dsc-loadmore").hidden = true;
    } catch (_) {
      btn.disabled = false; btn.textContent = "Try again";
    }
  }

  /* keep the follower number on a card in step with the Follow button */
  const fmt = (n) => (n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e4 ? `${Math.round(n / 1e3)}K` : n >= 1e3 ? `${(n / 1e3).toFixed(1)}K` : String(n));
  document.addEventListener("cirq:follow-changed", (e) => {
    const { username, followers } = e.detail || {};
    if (!username || typeof followers !== "number") return;
    document.querySelectorAll(`[data-dsc-followers="${CSS.escape(username)}"]`).forEach((el) => { el.textContent = fmt(followers); el.dataset.count = followers; });
  });
  syncInput();
})();
