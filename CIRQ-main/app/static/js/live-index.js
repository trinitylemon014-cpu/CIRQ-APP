(() => {
  "use strict";
  const grid = document.getElementById("lv-grid");
  if (!grid) return;
  const csrf = () => (document.querySelector('meta[name="csrf-token"]') || {}).content || "";
  const roomUrl = (id) => grid.dataset.roomUrl.replace("SID", id);
  let cat = "", timer = null, inflight = false;
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };

  function card(s) {
    const a = el("a", "lv-card"); a.href = roomUrl(s.id);
    a.setAttribute("aria-label", `${s.host.display_name} is live: ${s.title}`);
    if (s.host.avatar) { const bg = el("img", "lv-bg"); bg.src = s.host.avatar; bg.alt = ""; bg.loading = "lazy"; a.appendChild(bg); }
    const badges = el("div", "lv-badges");
    badges.appendChild(el("span", "lv-live", "LIVE"));
    if (s.mode === "audio") badges.appendChild(el("span", "lv-pill", "Audio only"));
    if (s.visibility === "followers") badges.appendChild(el("span", "lv-pill", "Followers only"));
    if (s.mine) badges.appendChild(el("span", "lv-pill", "Your live"));
    else if (s.followed) badges.appendChild(el("span", "lv-pill", "Following"));
    badges.appendChild(el("span", "lv-pill lv-viewers", `${s.viewers} watching`));
    a.appendChild(badges);
    a.appendChild(el("h3", "lv-title", s.title));
    const host = el("div", "lv-host"), av = el("span", "lv-av");
    if (s.host.avatar) { const i = el("img"); i.src = s.host.avatar; i.alt = ""; av.appendChild(i); } else av.textContent = (s.host.username || "?")[0].toUpperCase();
    host.appendChild(av);
    host.appendChild(el("span", "", s.host.display_name + (s.host.verified ? " ✓" : "")));
    host.appendChild(el("span", "lv-pill", s.category));
    a.appendChild(host);
    return a;
  }

  function message(kind, title, text, action) {
    grid.replaceChildren();
    const box = el("div", "card " + (kind === "error" ? "lv-err" : "lv-empty")); box.style.gridColumn = "1/-1";
    box.appendChild(el("h3", "", title)); box.appendChild(el("p", "muted", text));
    if (action) box.appendChild(action);
    grid.appendChild(box);
  }

  async function load() {
    if (inflight) return; inflight = true;
    try {
      const r = await fetch("/live/api/sessions" + (cat ? "?category=" + encodeURIComponent(cat) : ""), { headers: { "X-Requested-With": "fetch", "X-CSRFToken": csrf() }, credentials: "same-origin" });
      if (r.status === 401) { location.href = "/login"; return; }
      if (!r.ok) throw new Error("HTTP " + r.status);
      const d = await r.json();
      grid.setAttribute("aria-busy", "false");
      if (!d.sessions.length) {
        let act = null;
        if (grid.dataset.canHost === "1") { act = el("a", "btn", "Go live"); act.href = grid.dataset.goUrl; }
        message("empty", cat ? `Nobody is live in ${cat}` : "Nobody is live right now", "Follow creators to get a notification the moment they go live.", act);
      } else grid.replaceChildren(...d.sessions.map(card));
    } catch (e) {
      const b = el("button", "btn sm", "Try again"); b.type = "button"; b.addEventListener("click", load);
      message("error", "Couldn't load lives", "Check your connection and try again.", b);
    } finally { inflight = false; }
  }
  const schedule = () => { clearTimeout(timer); timer = setTimeout(async () => { if (!document.hidden) await load(); schedule(); }, +grid.dataset.refreshMs || 15000); };
  document.querySelectorAll(".lv-cats button").forEach((b) => b.addEventListener("click", () => {
    document.querySelectorAll(".lv-cats button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    cat = b.dataset.cat; load();
  }));
  document.addEventListener("visibilitychange", () => { if (!document.hidden) load(); });
  load(); schedule();
})();
