/* "Who liked this": one shared sheet for posts, reels and comments. Any element with data-likers="<url>" opens it.
   The server decides who may see the list (same rules as the post); this file only displays it. */
(() => {
  "use strict";
  const dlg = document.getElementById("likers-dialog");
  if (!dlg) return;
  const list = dlg.querySelector("[data-likers-list]"), more = dlg.querySelector("[data-likers-more]"), title = dlg.querySelector("[data-likers-title]");
  let url = "", next = null, busy = false, token = 0;

  async function load(reset) {
    if (busy && !reset) return;
    const mine = ++token;
    busy = true;
    if (reset) { list.innerHTML = '<div class="likers-loading" aria-hidden="true"><i></i><i></i><i></i></div>'; more.hidden = true; }
    more.disabled = true;
    try {
      const sep = url.includes("?") ? "&" : "?";
      const res = await fetch(url + (next ? `${sep}before=${encodeURIComponent(next)}` : ""), { credentials: "same-origin", headers: { "X-Requested-With": "fetch" } });
      if (!res.ok) throw new Error(res.status);
      const box = document.createElement("div");
      box.innerHTML = await res.text();
      if (mine !== token) return;                                   // a newer open replaced this request
      const marker = box.querySelector("[data-next]");
      next = marker ? marker.dataset.next : null;
      if (marker) marker.remove();
      if (reset) list.innerHTML = "";
      list.append(...box.childNodes);
      more.hidden = !next;
    } catch (err) {
      if (mine !== token) return;
      if (reset) list.innerHTML = '<p class="likers-empty muted">Couldn’t load the list. Close this and try again.</p>';
      else more.hidden = false;
    } finally {
      if (mine === token) { busy = false; more.disabled = false; }
    }
  }

  document.addEventListener("click", (event) => {
    const t = event.target.closest("[data-likers]");
    if (!t || t === dlg) return;
    event.preventDefault();
    url = t.dataset.likers; next = null;
    title.textContent = t.dataset.likersTitle || "Likes";
    if (!dlg.open) dlg.showModal();
    load(true);
  });
  more.addEventListener("click", () => load(false));
  dlg.addEventListener("click", (event) => { if (event.target === dlg) dlg.close(); });          // tap outside the sheet to close
  dlg.addEventListener("close", () => { token++; list.innerHTML = ""; });
})();
