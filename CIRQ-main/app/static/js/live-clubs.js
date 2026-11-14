(() => {
  "use strict";
  const csrf = () => (document.querySelector('meta[name="csrf-token"]') || {}).content || "";
  document.addEventListener("click", async (ev) => {
    const b = ev.target.closest("[data-leave]");
    if (!b) return;
    b.disabled = true;
    try {
      const r = await fetch(`/live/api/fanclub/${encodeURIComponent(b.dataset.leave)}/leave`, { method: "POST", credentials: "same-origin", headers: { "X-CSRFToken": csrf(), "X-Requested-With": "fetch" } });
      if (!r.ok) throw new Error();
      const row = b.closest("[data-club]"); if (row) row.remove();
    } catch (e) { b.disabled = false; b.textContent = "Try again"; }
  });
})();
