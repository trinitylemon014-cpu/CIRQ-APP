/* CIRQ front-end. No authorization logic lives here: the server decides everything. */
(() => {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const csrf = () => ($('meta[name="csrf-token"]') || {}).content || "";

  /* ---------- active status ---------- */
  function setPresence(el, isActive) {
    el.classList.toggle("is-active", isActive);
    el.setAttribute("aria-label", isActive ? "Active now" : "Offline");
  }
  function formatLastSeen(lastSeenAt) {
    const timestamp = Date.parse(lastSeenAt || "");
    if (!Number.isFinite(timestamp)) return "Offline";
    const minutes = Math.floor(Math.max(0, Date.now() - timestamp) / 60000);
    if (minutes < 60) return `Active ${Math.max(1, minutes)}m ago`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `Active ${hours}h ago`;
    if (hours < 48) return "Active yesterday";
    return `Active ${Math.floor(hours / 24)}d ago`;
  }
  if (document.body.dataset.presenceEnabled === "1") {
    let heartbeatTimer;
    const pingPresence = (finalPing = false) => {
      if (!finalPing && document.visibilityState !== "visible") return;
      fetch("/api/presence/ping", {
        method: "POST", keepalive: true, credentials: "same-origin",
        headers: { "X-CSRFToken": csrf(), "X-Requested-With": "fetch" },
      }).catch(() => {});
    };
    const scheduleHeartbeat = () => {
      clearTimeout(heartbeatTimer);
      if (document.visibilityState === "visible") heartbeatTimer = setTimeout(() => {
        pingPresence();
        scheduleHeartbeat();
      }, 60000);
    };
    const refreshPresence = async () => {
      if (document.visibilityState !== "visible") return;
      const markers = $$("[data-presence-dot], [data-presence-label]");
      const ids = [...new Set(markers.map((el) => el.dataset.userId).filter(Boolean))];
      for (let start = 0; start < ids.length; start += 80) {
        const params = new URLSearchParams();
        ids.slice(start, start + 80).forEach((id) => params.append("user_id", id));
        try {
          const response = await fetch(`/api/presence/status?${params}`, {
            credentials: "same-origin", headers: { "X-Requested-With": "fetch" },
          });
          if (!response.ok) continue;
          const { statuses = {}, last_seen_at: lastSeenAt = {} } = await response.json();
          markers.filter((el) => ids.slice(start, start + 80).includes(el.dataset.userId)).forEach((el) => {
            const state = statuses[el.dataset.userId];
            if (el.matches("[data-presence-dot]")) {
              el.hidden = !state || state === "hidden";
              setPresence(el, state === "active");
              el.title = state === "active" ? "Active now" : "Offline";
            } else {
              el.hidden = !state || state === "hidden";
              el.classList.toggle("is-active", state === "active");
              el.textContent = state === "active" ? "Active now" : formatLastSeen(lastSeenAt[el.dataset.userId]);
            }
          });
        } catch (_) {
          // Presence is decorative; a failed refresh must not affect the page.
        }
      }
    };
    const resumePresence = () => {
      if (document.visibilityState === "visible") {
        pingPresence();
        scheduleHeartbeat();
        refreshPresence();
      } else {
        clearTimeout(heartbeatTimer);
      }
    };
    pingPresence();
    scheduleHeartbeat();
    refreshPresence();
    document.addEventListener("visibilitychange", resumePresence);
    window.addEventListener("pagehide", () => pingPresence(true));
    setInterval(refreshPresence, 60000);
  }

  /* delivery ticks: while the app is open, tell the server my messages have arrived (works even if I hide my active status) */
  if (document.body.dataset.deliveryPing === "1") {
    const pingDelivered = () => {
      if (document.visibilityState !== "visible") return;
      fetch("/messages/delivered", {
        method: "POST", credentials: "same-origin",
        headers: { "X-CSRFToken": csrf(), "X-Requested-With": "fetch" },
      }).catch(() => {});
    };
    pingDelivered();
    setInterval(pingDelivered, 30000);
    document.addEventListener("visibilitychange", pingDelivered);
  }

  /* ---------- like counts live outside the like button so they can open the "who liked" list ---------- */
  function setLikeCount(id, n, like) {
    const own = like && $("[data-count]", like); if (own) own.textContent = n;
    $$(`[data-likes-of="${CSS.escape(String(id))}"]`).forEach((el) => {
      el.textContent = n;
      const lbl = el.parentElement && $(".act-lbl", el.parentElement); if (lbl) lbl.textContent = n === 1 ? " Like" : " Likes";
    });
  }
  function setCommentLikeCount(id, n, clike) {
    const own = clike && $("[data-count]", clike); if (own) own.textContent = n || "";
    $$(`[data-clikes-of="${CSS.escape(String(id))}"]`).forEach((el) => { el.textContent = n || ""; el.hidden = !n; });
  }

  /* ---------- post captions: show "…more" only when the text is actually clamped ---------- */
  function markClampedCaptions(root) {
    $$(".post-caption:not([data-clamp-checked])", root || document).forEach((el) => {
      if (!el.offsetParent) return;                       // hidden for now (e.g. editing): check again later
      el.dataset.clampChecked = "1";
      if (el.scrollHeight > el.clientHeight + 1) el.classList.add("is-clamped");
    });
  }
  document.addEventListener("click", (event) => {
    const more = event.target.closest("[data-caption-more]");
    if (more) { more.closest(".post-caption").classList.add("is-open"); }
  });
  markClampedCaptions();
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => { $$(".post-caption").forEach((el) => { delete el.dataset.clampChecked; el.classList.remove("is-clamped"); }); markClampedCaptions(); });
  if ("MutationObserver" in window) {
    let queued = false;
    new MutationObserver(() => { if (queued) return; queued = true; requestAnimationFrame(() => { queued = false; markClampedCaptions(); }); })
      .observe(document.body, { childList: true, subtree: true });
  }
  window.addEventListener("resize", () => { $$(".post-caption:not(.is-open)").forEach((el) => { el.classList.remove("is-clamped"); delete el.dataset.clampChecked; }); markClampedCaptions(); });

  /* ---------- responsive all-pages menu ---------- */
  const pageMenu = $("#mobile-page-menu"), menuBackdrop = $(".mobile-menu-backdrop");
  const menuTriggers = $$("[data-mobile-menu-trigger]");
  let lastMenuTrigger = null, menuCloseTimer;
  const menuFocusables = () => $$('a[href], button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])', pageMenu)
    .filter((el) => !el.hidden && el.getClientRects().length);
  function closePageMenu(restoreFocus = true) {
    if (!pageMenu || pageMenu.hidden) return;
    pageMenu.classList.remove("is-open");
    document.body.classList.remove("mobile-menu-open");
    menuTriggers.forEach((button) => button.setAttribute("aria-expanded", "false"));
    clearTimeout(menuCloseTimer);
    menuCloseTimer = setTimeout(() => {
      if (!pageMenu.classList.contains("is-open")) {
        pageMenu.hidden = true;
        menuBackdrop.hidden = true;
      }
    }, 210);
    if (restoreFocus && lastMenuTrigger) lastMenuTrigger.focus({ preventScroll: true });
  }
  function openPageMenu(trigger) {
    if (!pageMenu || !menuBackdrop) return;
    lastMenuTrigger = trigger;
    clearTimeout(menuCloseTimer);
    pageMenu.hidden = false;
    menuBackdrop.hidden = false;
    document.body.classList.add("mobile-menu-open");
    menuTriggers.forEach((button) => button.setAttribute("aria-expanded", "true"));
    requestAnimationFrame(() => pageMenu.classList.add("is-open"));
    const first = menuFocusables()[0];
    if (first) first.focus({ preventScroll: true });
  }
  menuTriggers.forEach((button) => button.addEventListener("click", () => {
    if (pageMenu && !pageMenu.hidden) closePageMenu();
    else openPageMenu(button);
  }));
  $$("[data-mobile-menu-close]").forEach((button) => button.addEventListener("click", () => closePageMenu()));
  pageMenu && pageMenu.addEventListener("click", (event) => {
    if (event.target.closest("a")) closePageMenu();
  });
  document.addEventListener("focusin", (event) => {
    if (pageMenu && pageMenu.classList.contains("is-open") && !pageMenu.contains(event.target)) {
      const first = menuFocusables()[0];
      if (first) first.focus({ preventScroll: true });
    }
  });
  document.addEventListener("keydown", (event) => {
    if (!pageMenu || pageMenu.hidden || !pageMenu.classList.contains("is-open")) return;
    if (event.key === "Escape") {
      event.preventDefault();
      closePageMenu();
      return;
    }
    if (event.key !== "Tab") return;
    const focusables = menuFocusables();
    if (!focusables.length) { event.preventDefault(); return; }
    const first = focusables[0], last = focusables[focusables.length - 1];
    if (event.shiftKey && (document.activeElement === first || !pageMenu.contains(document.activeElement))) {
      event.preventDefault(); last.focus();
    } else if (!event.shiftKey && (document.activeElement === last || !pageMenu.contains(document.activeElement))) {
      event.preventDefault(); first.focus();
    }
  });
  window.addEventListener("popstate", () => closePageMenu(false));
  window.addEventListener("pagehide", () => closePageMenu(false));

  const schoolChoice = $("#f-school-choice"), customSchool = $("[data-new-school]");
  if (schoolChoice && customSchool) {
    const schoolName = $("#f-new-school");
    const updateSchoolChoice = () => {
      const custom = schoolChoice.value === "My school isn't listed";
      customSchool.hidden = !custom;
      schoolName.disabled = !custom;
      schoolName.required = custom;
    };
    schoolChoice.addEventListener("input", updateSchoolChoice);
    schoolChoice.addEventListener("change", updateSchoolChoice);
    updateSchoolChoice();
  }

  const approvalPanel = $("[data-approval-status]");
  if (approvalPanel) {
    const countdown = $("[data-approval-countdown]", approvalPanel);
    const label = $("[data-approval-label]", approvalPanel);
    const waiting = $("[data-approval-waiting]", approvalPanel);
    const chip = $(".chip", approvalPanel);
    let remaining = Math.max(0, Number(approvalPanel.dataset.secondsRemaining) || 0);
    const renderCountdown = () => {
      const minutes = Math.floor(remaining / 60), seconds = remaining % 60;
      countdown.textContent = `${minutes}:${String(seconds).padStart(2, "0")}`;
      if (remaining === 0) waiting.hidden = false;
    };
    renderCountdown();
    setInterval(() => {
      if (remaining > 0) remaining -= 1;
      renderCountdown();
    }, 1000);
    const pollApproval = async () => {
      try {
        const response = await fetch(approvalPanel.dataset.statusUrl, {
          credentials: "same-origin",
          headers: { "X-Requested-With": "fetch" },
        });
        if (!response.ok) return;
        const state = await response.json();
        if (state.status === "approved") {
          window.location.replace("/feed");
          return;
        }
        if (state.status === "rejected") {
          label.textContent = "Not approved";
          waiting.textContent = state.status_note || "An administrator was unable to approve this account. Contact CIRQ support for help.";
          waiting.hidden = false;
          countdown.hidden = true;
          if (chip) { chip.classList.remove("warn"); chip.classList.add("red"); }
          return;
        }
        remaining = Math.max(0, Number(state.seconds_remaining) || 0);
        renderCountdown();
        if (!state.email_verified || state.auto_approval_blocked) waiting.hidden = false;
      } catch (_) {
        // Poll failures should never prevent the user from staying on the waiting page.
      }
    };
    pollApproval();
    setInterval(pollApproval, 5000);
  }

  async function api(url, { method = "POST", body, json } = {}) {
    const headers = { "X-CSRFToken": csrf(), "X-Requested-With": "fetch" };
    let payload = body;
    if (json !== undefined) { headers["Content-Type"] = "application/json"; payload = JSON.stringify(json); }
    const res = await fetch(url, { method, headers, body: method === "GET" ? undefined : payload, credentials: "same-origin" });
    let data = {};
    try { data = await res.json(); } catch (_) { /* non-JSON */ }
    if (!res.ok) { const err = new Error(data.error || "Something went wrong."); err.status = res.status; throw err; }
    return data;
  }
  function toast(msg, kind = "info") {
    let box = $(".flashes");
    if (!box) { box = document.createElement("div"); box.className = "flashes"; document.body.append(box); }
    const el = document.createElement("div");
    el.className = "flash " + kind;
    el.innerHTML = '<span></span><button type="button" data-dismiss aria-label="Dismiss">✕</button>';
    el.firstChild.textContent = msg;
    box.append(el);
    setTimeout(() => el.remove(), 5000);
  }
  setTimeout(() => $$(".flash").forEach((f) => f.remove()), 6000);
  window.CIRQ = Object.assign(window.CIRQ || {}, { api, toast, csrf });   // shared with comments.js

  /* ---------- viewing signals: what a student dwells on / watches (powers "Suggested for you") ---------- */
  const sig = { q: [], timer: null };
  function flushSignals() {
    clearTimeout(sig.timer); sig.timer = null;
    if (!sig.q.length) return;
    const events = sig.q.splice(0, 30);
    fetch("/api/signals", { method: "POST", keepalive: true, credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrf(), "X-Requested-With": "fetch" }, body: JSON.stringify({ events }) }).catch(() => {});
    if (sig.q.length) sig.timer = setTimeout(flushSignals, 1000);
  }
  function pushSignal(post_id, event, value) {
    if (!post_id) return;
    sig.q.push({ post_id, event, value });
    if (sig.q.length >= 20) flushSignals(); else if (!sig.timer) sig.timer = setTimeout(flushSignals, 6000);
  }
  window.addEventListener("pagehide", () => { dwellFlush(); flushSignals(); });

  // posts in the feed: count a "view" when a post stays mostly on screen for a moment
  const dwell = new Map();
  const dwellIO = "IntersectionObserver" in window ? new IntersectionObserver((entries) => {
    entries.forEach((en) => {
      const el = en.target;
      if (en.isIntersecting && en.intersectionRatio >= 0.6) { if (!dwell.has(el)) dwell.set(el, performance.now()); }
      else if (dwell.has(el)) { const ms = performance.now() - dwell.get(el); dwell.delete(el); if (ms >= 1500) pushSignal(el.dataset.postId, "view", Math.round(ms)); }
    });
  }, { threshold: [0, 0.6] }) : null;
  function dwellFlush() { dwell.forEach((t0, el) => { const ms = performance.now() - t0; if (ms >= 1500) pushSignal(el.dataset.postId, "view", Math.round(ms)); dwell.set(el, performance.now()); }); }
  document.addEventListener("visibilitychange", () => { if (document.hidden) { dwellFlush(); flushSignals(); } });
  function observePosts(root) { if (dwellIO) $$("article.post[data-post-id]", root || document).forEach((a) => { if (!a._d) { a._d = 1; dwellIO.observe(a); } }); }
  observePosts();
  const feedBox = $("#feed"); if (feedBox) new MutationObserver(() => observePosts(feedBox)).observe(feedBox, { childList: true });
  document.addEventListener("ended", (e) => {      // a post video watched to the end is a strong signal
    const v = e.target, card = v.closest && v.closest("article.post[data-post-id]"); if (card && v.tagName === "VIDEO") pushSignal(card.dataset.postId, "watch", 1);
  }, true);

  /* ---------- generic click handlers (event delegation) ---------- */
  document.addEventListener("click", async (e) => {
    const t = e.target;
    const c = (sel) => t.closest(sel);

    const cc = c("[data-confirm-click]");
    if (cc && !window.confirm(cc.dataset.confirmClick)) { e.preventDefault(); return; }
    if (c("[data-dismiss]")) { c(".flash").remove(); return; }
    if (c("[data-open]")) {      // opening a sheet from inside another one (e.g. Create -> Story) closes the first
      const b = c("[data-open]"), d = document.getElementById(b.dataset.open), cur = b.closest("dialog");
      if (cur && cur !== d) cur.close();
      d && d.showModal(); return;
    }
    const mt = c("[data-music-toggle]");
    if (mt) { toggleMusic(mt.closest("[data-music]")); return; }
    if (c("[data-close]")) { const d = c("dialog"); d && d.close(); return; }
    if (t.tagName === "DIALOG") { t.close(); return; }

    const menuBtn = c("[data-menu]");
    $$(".menu.open").forEach((m) => { if (!menuBtn || m !== menuBtn.closest(".menu")) m.classList.remove("open"); });
    if (menuBtn) {
      const menu = menuBtn.closest(".menu"), open = menu.classList.toggle("open");
      menuBtn.setAttribute("aria-expanded", open); return;
    }

    const pw = c("[data-toggle-pw]");
    if (pw) { const inp = document.getElementById(pw.dataset.togglePw); inp.type = inp.type === "password" ? "text" : "password"; return; }

    const cp = c("[data-copy]");
    if (cp) {
      try { await navigator.clipboard.writeText(cp.dataset.copy); toast("Link copied", "success"); }
      catch (_) { window.prompt("Copy this link:", cp.dataset.copy); }
      return;
    }

    const rep = c("[data-report]");
    if (rep) {
      e.preventDefault();
      const [type, id] = rep.dataset.report.split(":");
      const d = $("#report-dialog"); const f = $("[data-report-form]", d);
      f.target_type.value = type; f.target_id.value = id; d.showModal(); return;
    }

    const ni = c("[data-not-interested]");
    if (ni) {
      const id = ni.dataset.notInterested, card = c("[data-reel]") || c("article.post");
      try {
        const r = await api(`/posts/${id}/not-interested`);
        toast(r.message || "We'll show fewer posts like this.", "success");
        if (card) { card.classList.add("gone"); setTimeout(() => { if (card.matches("[data-reel]")) { const nx = card.nextElementSibling; nx && nx.scrollIntoView({ behavior: "smooth" }); setTimeout(() => card.remove(), 400); } else card.remove(); }, 300); }
      } catch (err) { toast(err.message, "error"); }
      return;
    }

    const clike = c("[data-clike]");
    if (clike) {             // like / unlike a comment on a post
      clike.disabled = true;
      try {
        const r = await api(`/comments/${clike.dataset.clike}/like`);
        clike.classList.toggle("liked", r.liked); clike.setAttribute("aria-pressed", r.liked);
        setCommentLikeCount(clike.dataset.clike, r.count, clike);
      } catch (err) { toast(err.message, "error"); }
      clike.disabled = false; return;
    }

    const like = c("[data-like]");
    if (like) {
      like.disabled = true;
      try {
        const r = await api(`/posts/${like.dataset.like}/like`);
        like.classList.toggle("liked", r.liked); like.setAttribute("aria-pressed", r.liked);
        setLikeCount(like.dataset.like, r.count, like);
      } catch (err) { toast(err.message, "error"); }
      like.disabled = false; return;
    }

    const save = c("[data-save]");
    if (save) {                // bookmark / un-bookmark a post or reel (reel viewer, post cards and the Saved grid)
      if (save.disabled) return;
      save.disabled = true;
      try {
        const r = await api(`/posts/${save.dataset.save}/save`);
        $$(`[data-save="${save.dataset.save}"]`).forEach((b) => {   // keep every copy of this post's button in sync
          b.classList.toggle("saved", r.saved); b.setAttribute("aria-pressed", r.saved);
          b.setAttribute("aria-label", r.saved ? "Remove from saved" : "Save");
          const lbl = $("[data-save-label]", b); if (lbl) lbl.textContent = r.saved ? "Saved" : "Save";
        });
        const tile = save.closest("[data-saved-tile]");
        if (tile && !r.saved) {          // un-saving from the Saved grid removes the tile right away
          const grid = tile.parentElement;
          tile.classList.add("gone");
          setTimeout(() => {
            tile.remove();
            if (!$("[data-saved-tile]", grid)) { const e = $("[data-saved-empty]"); if (e && !$("[data-more-cursor]", grid)) e.hidden = false; }
          }, 260);
        }
        toast(r.saved ? "Saved to your profile" : "Removed from saved", "success");
      } catch (err) { toast(err.message, "error"); }
      save.disabled = false; return;
    }

    const fol = c("[data-follow]");
    if (fol) {
      const who = fol.dataset.follow, state0 = fol.dataset.state || "none";
      // Instagram asks before you drop out of a private account (you'd have to ask again), and before cancelling a request.
      if (state0 === "following" && fol.dataset.private === "1" && !confirm(`If you unfollow @${who}, you'll need to send a new request to follow them again.`)) return;
      fol.disabled = true;
      try {
        const r = await api(`/u/${who}/follow`);
        const label = { none: "Follow", requested: "Requested", following: "Following" }[r.state] || "Follow";
        $$(`[data-follow="${who}"]`).forEach((b) => { b.textContent = label; b.classList.toggle("following", r.state !== "none"); b.dataset.state = r.state; });
        const n = $("[data-followers]"); if (n) n.textContent = r.followers;
        if (r.state === "requested") toast(`Request sent to @${who}`, "success");
        document.dispatchEvent(new CustomEvent("cirq:follow-changed", { detail: { username: who, state: r.state, followers: r.followers } }));
      } catch (err) { toast(err.message, "error"); }
      fol.disabled = false; return;
    }

    const reqBtn = c("[data-req-approve],[data-req-decline]");
    if (reqBtn) {            // follow requests page: Confirm / Delete
      const approve = reqBtn.hasAttribute("data-req-approve"), who = reqBtn.dataset.reqApprove || reqBtn.dataset.reqDecline, row = c("[data-request]");
      $$("button", row).forEach((b) => (b.disabled = true));
      try {
        const r = await api(`/follow-requests/${who}/${approve ? "approve" : "decline"}`);
        row.remove(); toast(approve ? `@${who} can now follow you and chat with you.` : "Request deleted.", approve ? "success" : "info");
        const empty = $("[data-requests-empty]"); if (empty && !r.remaining) empty.hidden = false;
      } catch (err) { $$("button", row).forEach((b) => (b.disabled = false)); toast(err.message, "error"); }
      return;
    }

    const edit = c("[data-edit-post]");
    if (edit) { const card = c(".post"); $("[data-edit-form]", card).hidden = false; const cap = $("[data-caption]", card); cap && (cap.hidden = true); card.querySelector(".menu").classList.remove("open"); return; }
    if (c("[data-cancel-edit]")) { const card = c(".post"); $("[data-edit-form]", card).hidden = true; const cap = $("[data-caption]", card); cap && (cap.hidden = false); return; }

    const story = c("[data-story]");
    if (story) { openStory(story.dataset.story); return; }

    const more = c("#load-more");
    if (more) { loadMore(more); return; }
  });

  document.addEventListener("submit", async (e) => {
    const f = e.target;
    if (f.dataset.confirm && !window.confirm(f.dataset.confirm)) { e.preventDefault(); return; }
    if (f.matches("[data-report-form]")) {
      e.preventDefault();
      try { const r = await api(f.action, { body: new FormData(f) }); toast(r.message, "success"); f.closest("dialog").close(); f.reset(); }
      catch (err) { toast(err.message, "error"); }
      return;
    }
    if (f.matches("[data-story-form]")) {
      if (f.elements.story_type.value === "text") {
        if (!f.elements.text.value.trim()) { e.preventDefault(); toast("Write something for your story first.", "error"); $("[data-compose-text]", f).focus(); return; }
      } else if (!f.elements.media.files[0]) { e.preventDefault(); toast("Choose a photo or video first, or open the camera.", "error"); return; }
      const b = $("button[type=submit]", f); b.disabled = true; b.textContent = "Sharing…";   // the page redirects when the upload is done
      return;
    }
    if (f.matches("[data-comment-form]")) {
      e.preventDefault();
      const btn = $("button", f); btn.disabled = true;
      try {
        const r = await api(f.action, { body: new FormData(f) });
        const list = $("[data-comment-list]"); const none = $("[data-no-comments]"); none && none.remove();
        list.insertAdjacentHTML("beforeend", r.html); f.reset();
        const n = $("[data-comment-count]"); n && (n.textContent = `(${r.count})`);
      } catch (err) { toast(err.message, "error"); }
      btn.disabled = false; return;
    }
    if (f.matches('[action$="/delete"][data-ajax]')) { /* reserved */ }
  });

  document.addEventListener("change", (e) => {
    const t = e.target;
    if (t.matches('input[type="file"]')) {
      const lbl = t.closest("label") && $("[data-file-label]", t.closest("label"));
      if (lbl && t.files[0]) lbl.textContent = t.files[0].name;
      if (t.dataset.autosubmit && t.files[0]) document.getElementById(t.dataset.autosubmit).submit();
    }
    if (t.matches('input[type="file"][name="audio"]')) {         // reveal the song-title box once a clip is chosen
      const f = t.closest("form"), ti = f && $("[data-audio-title]", f);
      if (ti) ti.hidden = !t.files[0];
    }
    if (t.matches('input[type="file"][data-preview]')) previewMedia(t);
    if (t.matches("[data-toggle-manual]")) {
      $("#manual-block").hidden = !t.checked; $("#email-block").hidden = t.checked;
    }
  });

  /* ---------- infinite "load more" ---------- */
  async function loadMore(btn) {
    const target = $(btn.dataset.target);
    const marker = $$("[data-more-cursor]", target).pop();
    if (!marker) { btn.hidden = true; return; }
    const url = new URL(target.dataset.feedUrl, location.origin);
    url.searchParams.set("before", marker.dataset.moreCursor); url.searchParams.set("partial", "1");
    btn.disabled = true; btn.textContent = "Loading…";
    try {
      const res = await fetch(url, { credentials: "same-origin", headers: { "X-Requested-With": "fetch" } });
      const html = await res.text(); marker.remove(); target.insertAdjacentHTML("beforeend", html);
      btn.hidden = !$$("[data-more-cursor]", target).length;
    } catch (_) { toast("Couldn't load more.", "error"); }
    btn.disabled = false; btn.textContent = "Load more";
  }

  /* ---------- music on posts: one clip plays at a time ---------- */
  let playingMusic = null;
  function setMusicState(box, on) {
    box.classList.toggle("playing", on);
    const btn = $("[data-music-toggle]", box);
    btn.setAttribute("aria-label", on ? "Pause music" : "Play music");
    btn.innerHTML = SVG(on ? "stop" : "play", "i-sm");
  }
  function toggleMusic(box) {
    const audio = $("audio", box);
    if (playingMusic && playingMusic !== box) { $("audio", playingMusic).pause(); setMusicState(playingMusic, false); playingMusic = null; }
    if (audio.paused) audio.play().then(() => { playingMusic = box; setMusicState(box, true); }).catch(() => toast("Couldn't play this music.", "error"));
    else { audio.pause(); setMusicState(box, false); playingMusic = null; }
  }
  document.addEventListener("ended", (e) => {      // media events don't bubble, so listen in the capture phase
    if (e.target.matches && e.target.matches("[data-music] audio")) { setMusicState(e.target.closest("[data-music]"), false); if (playingMusic === e.target.closest("[data-music]")) playingMusic = null; }
  }, true);

  /* ---------- text stories: shared sizing + the composer ---------- */
  // Shorter text gets bigger type, so a one-liner looks bold and a paragraph still fits. Used by the composer and the viewer.
  const storyFont = (n) => (n <= 30 ? 2.3 : n <= 70 ? 1.85 : n <= 120 ? 1.5 : n <= 200 ? 1.25 : 1.1);
  function setStoryMode(form, mode) {
    $("[data-story-type]", form).value = mode;
    $$("[data-story-mode]", form).forEach((b) => b.setAttribute("aria-selected", String(b.dataset.storyMode === mode)));
    $$("[data-story-panel]", form).forEach((p) => {
      const on = p.dataset.storyPanel === mode; p.hidden = !on;
      $$("input, textarea", p).forEach((i) => { i.disabled = !on; });        // a hidden panel never submits (no stray file or text)
    });
    if (mode === "text") { const t = $("[data-compose-text]", form); fitCompose(t); setTimeout(() => t.focus({ preventScroll: true }), 40); }
  }
  function fitCompose(t) {
    t.style.fontSize = `${storyFont(t.value.length)}rem`;
    t.style.height = "auto"; t.style.height = `${Math.min(t.scrollHeight, 270)}px`;
    const c = $("[data-compose-count]", t.closest("form")); c.textContent = `${t.value.length}/${t.maxLength}`; c.classList.toggle("warn", t.value.length >= t.maxLength - 20);
  }
  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-story-mode]"); if (b) setStoryMode(b.closest("form"), b.dataset.storyMode);
  });
  document.addEventListener("input", (e) => { if (e.target.matches("[data-compose-text]")) fitCompose(e.target); });
  document.addEventListener("change", (e) => {
    if (!e.target.matches(".bg-picker input")) return;
    const box = $("[data-compose]", e.target.closest("form")); box.className = `story-compose bg-${e.target.value}`;
  });

  /* ---------- preview of a chosen / recorded photo or video (story composer) ---------- */
  function previewMedia(input) {
    const box = $("[data-media-preview]", input.closest("form")); if (!box) return;
    if (box._url) URL.revokeObjectURL(box._url);
    box._url = null; box.textContent = "";
    const f = input.files[0]; if (!f) { box.hidden = true; return; }
    box._url = URL.createObjectURL(f);
    const isVideo = /^video\//.test(f.type) || /\.(mp4|mov|m4v|webm)$/i.test(f.name);
    const el = document.createElement(isVideo ? "video" : "img");
    el.src = box._url;
    if (isVideo) { el.muted = true; el.loop = true; el.playsInline = true; el.autoplay = true; } else el.alt = "Preview";
    box.append(el); box.hidden = false;
  }

  /* ---------- stories: view, like, comment, music, reply to the owner's inbox, owner insights ---------- */
  let viewer;
  const S = { items: [], i: 0, timer: null, user: null, t0: 0, dur: 5000, left: 0, paused: false };
  const SVG = (n, cls = "") => `<svg class="i ${cls}" aria-hidden="true"><use href="#i-${n}"/></svg>`;
  function ensureViewer() {
    if (viewer) return viewer;
    viewer = document.createElement("div"); viewer.className = "story-viewer"; viewer.setAttribute("role", "dialog"); viewer.setAttribute("aria-label", "Story");
    viewer.innerHTML = `<div class="story-box"><div class="story-bars"></div><div class="story-head"><span class="av" style="--s:38px"></span><b data-su></b><span class="grow"></span><button class="icon-btn" data-sdel hidden aria-label="Delete story" style="color:#fff">${SVG("trash")}</button><button class="icon-btn" data-sclose aria-label="Close" style="color:#fff">${SVG("x")}</button></div>
      <div data-smedia style="width:100%;height:100%"></div><div class="story-cap" data-scap></div>
      <button class="story-music" type="button" data-smusic hidden aria-label="Pause or play music">${SVG("music", "i-sm")}<span data-smtitle></span></button>
      <button class="story-hit prev" aria-label="Previous"></button><button class="story-hit next" aria-label="Next"></button>
      <div class="story-foot">
        <form class="story-reply" data-sreply hidden autocomplete="off"><input name="body" maxlength="500" placeholder="Send message…" aria-label="Reply to this story"><button class="icon-btn" aria-label="Send reply">${SVG("send")}</button></form>
        <button class="story-like" type="button" data-slike hidden aria-label="Like story" aria-pressed="false">${SVG("heart")}</button>
        <button class="story-like story-cbtn" type="button" data-scomments aria-label="Comments">${SVG("comment")}<b data-scc>0</b></button>
        <button class="story-stats" type="button" data-sinsights hidden aria-label="See who viewed and liked this story">${SVG("eye")}<b data-sviews>0</b>${SVG("heart")}<b data-slikes>0</b></button>
      </div>
      <div class="story-sheet" data-ssheet hidden>
        <div class="row between"><b>Story activity</b><button class="icon-btn" type="button" data-ssheet-close aria-label="Close activity">${SVG("x")}</button></div>
        <div class="tabs mini"><button type="button" class="active" data-stab="viewers">Viewers <span data-tv>0</span></button><button type="button" data-stab="likes">Likes <span data-tl>0</span></button></div>
        <div class="sheet-list" data-slist></div>
      </div>
      <div class="story-sheet" data-csheet hidden>
        <div class="row between"><b>Comments <span class="muted" data-ccount></span></b><button class="icon-btn" type="button" data-csheet-close aria-label="Close comments">${SVG("x")}</button></div>
        <div class="sheet-list" data-clist></div>
        <form class="story-reply cform" data-cform autocomplete="off"><input name="body" maxlength="500" placeholder="Add a comment…" aria-label="Add a comment"><button class="icon-btn" aria-label="Send comment">${SVG("send")}</button></form>
      </div></div>`;
    document.body.append(viewer);
    $("[data-sclose]", viewer).onclick = closeStory;
    viewer.addEventListener("click", (e) => { if (e.target === viewer) closeStory(); });
    document.addEventListener("keydown", (e) => {
      if (!viewer.classList.contains("open")) return;
      if (e.key === "Escape") { if (sheetOpen()) closeSheet(); else closeStory(); return; }
      if (/INPUT|TEXTAREA/.test(e.target.tagName || "")) return;    // typing a reply: arrows move the cursor, not the story
      if (e.key === "ArrowRight") step(1); if (e.key === "ArrowLeft") step(-1);
    });
    $(".next", viewer).onclick = () => step(1); $(".prev", viewer).onclick = () => step(-1);
    $("[data-sdel]", viewer).onclick = async () => {
      if (!confirm("Delete this story?")) return;
      try { await api(`/stories/${S.items[S.i].id}/delete`); toast("Story deleted", "success"); closeStory(); location.reload(); } catch (err) { toast(err.message, "error"); }
    };
    // like
    $("[data-slike]", viewer).onclick = async (e) => {
      const btn = e.currentTarget, s = S.items[S.i], want = !s.liked;
      s.liked = want; paintLike(btn, want);                                // instant, rolled back if the server says no
      try { const r = await api(`/stories/${s.id}/like`); s.liked = r.liked; paintLike(btn, r.liked); }
      catch (err) { s.liked = !want; paintLike(btn, !want); toast(err.message, "error"); }
    };
    // reply -> the owner's inbox. The story waits (like Instagram) while you type.
    const form = $("[data-sreply]", viewer), input = $("input", form);
    input.addEventListener("focus", pauseStory);
    input.addEventListener("blur", () => { if (!sheetOpen()) resumeStory(); });
    form.addEventListener("submit", async (e) => {
      e.preventDefault(); const body = input.value.trim(); if (!body) return;
      const btn = $("button", form); btn.disabled = true;
      try { await api(`/stories/${S.items[S.i].id}/reply`, { body: new FormData(form) }); input.value = ""; input.blur(); toast(`Reply sent to @${S.user.user.username}`, "success"); }
      catch (err) { toast(err.message, "error"); }
      btn.disabled = false;
    });
    // comments
    $("[data-scomments]", viewer).onclick = openComments;
    $("[data-csheet-close]", viewer).onclick = () => closeSheet();
    const cform = $("[data-cform]", viewer), cinput = $("input", cform);
    cform.addEventListener("submit", async (e) => {
      e.preventDefault(); if (!cinput.value.trim()) return;
      const btn = $("button", cform); btn.disabled = true;
      try { const r = await api(`/stories/${S.items[S.i].id}/comments`, { body: new FormData(cform) }); cinput.value = ""; addCommentRow(r.comment, true); bumpComments(1); }
      catch (err) { toast(err.message, "error"); }
      btn.disabled = false;
    });
    // music chip: tap to pause / play the story's music
    $("[data-smusic]", viewer).onclick = toggleStoryMusic;
    // owner insights
    $("[data-sinsights]", viewer).onclick = openInsights;
    $("[data-ssheet-close]", viewer).onclick = closeSheet;
    $$("[data-stab]", viewer).forEach((b) => b.addEventListener("click", () => { $$("[data-stab]", viewer).forEach((x) => x.classList.toggle("active", x === b)); renderList(b.dataset.stab); }));
    return viewer;
  }
  const paintLike = (btn, on) => { btn.classList.toggle("on", on); btn.setAttribute("aria-pressed", on ? "true" : "false"); btn.setAttribute("aria-label", on ? "Unlike story" : "Like story"); };
  async function openStory(username) {
    ensureViewer();
    try {
      const r = await api(`/stories/api/${username}`, { method: "GET" });
      if (!r.stories.length) { toast("No active stories", "info"); return; }
      S.items = r.stories; S.i = Math.max(0, r.stories.findIndex((s) => !s.seen)); S.user = r;
      $("[data-su]", viewer).textContent = r.user.username; const av = $(".av", viewer);
      av.textContent = ""; if (r.user.avatar) { const im = document.createElement("img"); im.src = r.user.avatar; im.alt = ""; av.append(im); } else av.textContent = r.user.username[0];
      $("[data-sdel]", viewer).hidden = !r.me;
      $("[data-sreply]", viewer).hidden = r.me || !r.can_reply;
      $("[data-sreply] input", viewer).placeholder = `Reply to ${r.user.username}…`;
      $("[data-slike]", viewer).hidden = r.me; $("[data-sinsights]", viewer).hidden = !r.me;
      viewer.classList.add("open"); document.body.style.overflow = "hidden"; show();
    } catch (err) { toast(err.message, "error"); }
  }
  function show() {
    clearTimeout(S.timer); S.paused = false; closeSheet(true);
    const s = S.items[S.i]; const bars = $(".story-bars", viewer);
    bars.innerHTML = S.items.map((_, k) => `<i><b style="width:${k < S.i ? 100 : 0}%"></b></i>`).join("");
    const holder = $("[data-smedia]", viewer); holder.innerHTML = "";
    if (s.type === "text") {
      const box = document.createElement("div"), p = document.createElement("p"), txt = s.text || "";
      box.className = `story-text bg-${s.bg || "violet"}`; p.textContent = txt; p.style.fontSize = `${storyFont(txt.length)}rem`;
      box.append(p); holder.append(box); run(Math.min(12000, 5000 + txt.length * 35));      // a little longer to read than a photo
    } else if (s.type === "video") {
      const v = document.createElement("video"); v.src = s.src; v.autoplay = true; v.playsInline = true; v.controls = false; holder.append(v);
      v.onloadedmetadata = () => run(Math.min(30000, (v.duration || 8) * 1000)); v.onended = () => { if (!S.paused) step(1); }; v.onerror = () => run(4000);
    } else { const im = document.createElement("img"); im.src = s.src; im.alt = "Story"; holder.append(im); run(5000); }
    $("[data-scap]", viewer).textContent = s.caption || "";
    paintLike($("[data-slike]", viewer), !!s.liked);
    $("[data-sviews]", viewer).textContent = s.views ?? 0; $("[data-slikes]", viewer).textContent = s.likes ?? 0;
    $("[data-scc]", viewer).textContent = s.comments || 0;
    startStoryMusic(s);
    $("[data-sreply] input", viewer).value = "";
    api(`/stories/${s.id}/view`).catch(() => {});
  }
  function run(ms) {
    const b = $$(".story-bars b", viewer)[S.i]; if (!b) return;
    S.t0 = performance.now(); S.dur = ms;
    b.style.transition = `width ${ms}ms linear`; requestAnimationFrame(() => requestAnimationFrame(() => (b.style.width = "100%")));
    S.timer = setTimeout(() => step(1), ms);
  }
  function pauseStory() {      // freeze the timer, progress bar and video exactly where they are
    if (S.paused || !viewer.classList.contains("open")) return; S.paused = true; clearTimeout(S.timer);
    S.left = Math.max(400, S.dur - (performance.now() - S.t0));
    const b = $$(".story-bars b", viewer)[S.i]; if (b) { const w = getComputedStyle(b).width; b.style.transition = "none"; b.style.width = w; }
    const v = $("video", viewer); v && v.pause();
    S.audio && S.audio.pause();
  }
  function resumeStory() {
    if (!S.paused || !viewer.classList.contains("open")) return; S.paused = false;
    const v = $("video", viewer); v && v.play().catch(() => {});
    const chip = $("[data-smusic]", viewer); if (S.audio && !chip.classList.contains("off")) S.audio.play().catch(() => {});
    const b = $$(".story-bars b", viewer)[S.i];
    if (b) { S.t0 = performance.now(); S.dur = S.left; b.style.transition = `width ${S.left}ms linear`; requestAnimationFrame(() => requestAnimationFrame(() => (b.style.width = "100%"))); S.timer = setTimeout(() => step(1), S.left); }
  }
  /* ---- story music ---- */
  function stopStoryMusic() { if (S.audio) { S.audio.pause(); S.audio = null; } const chip = $("[data-smusic]", viewer); if (chip) chip.hidden = true; }
  function startStoryMusic(s) {
    stopStoryMusic(); if (!s.audio) return;
    const chip = $("[data-smusic]", viewer); chip.hidden = false; chip.classList.remove("off"); $("[data-smtitle]", viewer).textContent = s.audio_title || "Music";
    const a = new Audio(s.audio); a.loop = true; a.volume = 0.9; S.audio = a;
    a.play().catch(() => chip.classList.add("off"));     // the browser blocked autoplay: the chip shows a muted look, tap it to start
  }
  function toggleStoryMusic() {
    const a = S.audio; if (!a) return; const chip = $("[data-smusic]", viewer);
    if (a.paused) a.play().then(() => chip.classList.remove("off")).catch(() => {}); else { a.pause(); chip.classList.add("off"); }
  }

  /* ---- story comments (visible to everyone who can see the story) ---- */
  function bumpComments(d) {
    const s = S.items[S.i]; s.comments = Math.max(0, (s.comments || 0) + d);
    $("[data-scc]", viewer).textContent = s.comments; $("[data-ccount]", viewer).textContent = s.comments ? `(${s.comments})` : "";
  }
  function emptyComments(list) { const p = document.createElement("p"); p.className = "muted"; p.dataset.cnone = "1"; p.textContent = "No comments yet. Say something nice."; list.append(p); }
  async function openComments() {
    pauseStory(); $("[data-csheet]", viewer).hidden = false;
    const list = $("[data-clist]", viewer); list.textContent = "Loading…";
    try {
      const r = await api(`/stories/${S.items[S.i].id}/comments`, { method: "GET" });
      list.textContent = ""; if (!r.comments.length) emptyComments(list);
      r.comments.forEach((c) => addCommentRow(c, false));
      S.items[S.i].comments = r.count; bumpComments(0);
    } catch (err) { list.textContent = err.message; }
  }
  function addCommentRow(c, scroll) {   // everything from the server is set as text, never as HTML
    const list = $("[data-clist]", viewer); const none = $("[data-cnone]", list); none && none.remove();
    const row = document.createElement("div"); row.className = "scomment"; row.dataset.cid = c.id;
    const av = document.createElement("span"); av.className = "av"; av.style.setProperty("--s", "34px");
    if (c.user.avatar) { const im = document.createElement("img"); im.src = c.user.avatar; im.alt = ""; av.append(im); } else av.textContent = c.user.username[0];
    const meta = document.createElement("div"); meta.className = "grow";
    const who = document.createElement("b"); who.textContent = c.user.username;
    const ago = document.createElement("small"); ago.className = "muted"; ago.textContent = ` ${c.ago}`;
    const body = document.createElement("div"); body.className = "scbody"; body.textContent = c.body;
    meta.append(who, ago, body);
    const like = document.createElement("button"); like.type = "button"; like.className = "scl" + (c.liked ? " on" : ""); like.setAttribute("aria-label", "Like comment");
    like.innerHTML = SVG("heart", "i-sm") + "<b></b>"; $("b", like).textContent = c.likes || 0;
    like.onclick = async () => {
      like.disabled = true;
      try { const r = await api(`/stories/comments/${c.id}/like`); like.classList.toggle("on", r.liked); $("b", like).textContent = r.count; }
      catch (err) { toast(err.message, "error"); }
      like.disabled = false;
    };
    row.append(av, meta, like);
    if (!c.mine) { const rep = document.createElement("button"); rep.type = "button"; rep.className = "scl"; rep.setAttribute("aria-label", "Report comment"); rep.dataset.report = `story_comment:${c.id}`; rep.innerHTML = SVG("flag", "i-sm"); row.append(rep); }
    if (c.can_delete) {
      const del = document.createElement("button"); del.type = "button"; del.className = "scl"; del.setAttribute("aria-label", "Delete comment"); del.innerHTML = SVG("trash", "i-sm");
      del.onclick = async () => {
        if (!confirm("Delete this comment?")) return;
        try { await api(`/stories/comments/${c.id}/delete`); row.remove(); bumpComments(-1); if (!list.children.length) emptyComments(list); }
        catch (err) { toast(err.message, "error"); }
      };
      row.append(del);
    }
    list.append(row); if (scroll) list.scrollTop = list.scrollHeight;
  }

  let insights = null;
  async function openInsights() {
    pauseStory(); const sh = $("[data-ssheet]", viewer); sh.hidden = false;
    $$("[data-stab]", viewer).forEach((x) => x.classList.toggle("active", x.dataset.stab === "viewers"));
    const list = $("[data-slist]", viewer); list.textContent = "Loading…"; insights = null;
    try { insights = await api(`/stories/${S.items[S.i].id}/insights`, { method: "GET" }); $("[data-tv]", viewer).textContent = insights.views; $("[data-tl]", viewer).textContent = insights.likes; renderList("viewers"); }
    catch (err) { list.textContent = err.message; }
  }
  function renderList(kind) {   // names come from the server: always set as text, never as HTML
    const list = $("[data-slist]", viewer); list.textContent = ""; if (!insights) return;
    const rows = kind === "likes" ? insights.likers : insights.viewers;
    if (!rows.length) { const p = document.createElement("p"); p.className = "muted"; p.textContent = kind === "likes" ? "No likes yet." : "No views yet."; list.append(p); return; }
    rows.forEach((u) => {
      const row = document.createElement("a"); row.className = "person"; row.href = `/u/${u.username}`;
      const av = document.createElement("span"); av.className = "av"; av.style.setProperty("--s", "40px");
      if (u.avatar) { const im = document.createElement("img"); im.src = u.avatar; im.alt = ""; av.append(im); } else av.textContent = u.username[0];
      const meta = document.createElement("div"); meta.className = "grow"; const b = document.createElement("b"); b.textContent = u.name; const sm = document.createElement("small"); sm.className = "muted"; sm.textContent = `@${u.username} · ${u.ago}`;
      meta.append(b, document.createElement("br"), sm); row.append(av, meta);
      if (u.liked) { const h = document.createElement("span"); h.className = "liked-heart"; h.innerHTML = SVG("heart", "fill"); h.title = "Liked"; row.append(h); }
      list.append(row);
    });
  }
  const sheetOpen = () => $$("[data-ssheet],[data-csheet]", viewer).some((x) => !x.hidden);
  function closeSheet(silent) { const open = $$("[data-ssheet],[data-csheet]", viewer).filter((x) => !x.hidden); if (!open.length) return; open.forEach((x) => (x.hidden = true)); if (!silent) resumeStory(); }
  function step(d) { S.i += d; if (S.i < 0) S.i = 0; if (S.i >= S.items.length) closeStory(); else show(); }
  function closeStory() { clearTimeout(S.timer); S.paused = false; stopStoryMusic(); closeSheet(true); viewer.classList.remove("open"); $("[data-smedia]", viewer).innerHTML = ""; document.body.style.overflow = ""; }

  /* ---------- reels feed: Instagram-style playback ---------- */
  const reels = $("#reels");
  if (reels) {
    /* size the player to the space really left on this screen so a whole reel is always visible */
    const reelWrap = reels.closest(".reel-wrap") || reels;
    let fitFrame = 0, fitDone = false;
    function fitReels() {
      const vv = window.visualViewport, vh = Math.round(vv ? vv.height : window.innerHeight);
      const nav = document.querySelector(".bottom-nav");
      const navStyle = nav && getComputedStyle(nav);
      const navTop = nav && navStyle.display !== "none" ? nav.getBoundingClientRect().top : vh;
      const covered = Math.max(0, vh - Math.min(vh, navTop)) || 0;                    // bottom bar (phones/tablets), 0 on desktop
      const top = reelWrap.getBoundingClientRect().top;
      let room = vh - top - covered - 14;
      if (room < 300) {                                                                 // very short screen: let the page scroll the player into view
        room = Math.max(280, vh - covered - 20);
        if (!fitDone) { fitDone = true; reelWrap.scrollIntoView({ block: "start" }); }
      }
      const next = Math.round(Math.min(room, 920));
      const old = reels.clientHeight || next, idx = old ? Math.round(reels.scrollTop / old) : 0;
      if (Math.abs(next - old) < 2 && reels.style.getPropertyValue("--reel-h")) return;
      reels.style.setProperty("--reel-h", next + "px");
      reelWrap.style.setProperty("--reel-h", next + "px");
      reels.scrollTop = idx * next;                                                     // stay on the same reel after a resize/rotate
    }
    const queueFit = () => { cancelAnimationFrame(fitFrame); fitFrame = requestAnimationFrame(fitReels); };
    fitReels();
    window.addEventListener("resize", queueFit);
    window.addEventListener("orientationchange", () => setTimeout(queueFit, 250));
    if (window.visualViewport) window.visualViewport.addEventListener("resize", queueFit);
    window.addEventListener("load", queueFit);
    let muted = true;
    const reportedReelViews = new Set();
    const VIEW_MIN_SECONDS = Number($("[data-reel]", reels)?.dataset.viewMinSeconds || 3);
    const formatReelCount = (value) => {
      const count = Math.max(0, Number(value) || 0);
      if (count < 1000) return Math.floor(count).toLocaleString();
      if (count < 10000) return `${(count / 1000).toFixed(1).replace(/\.0$/, "")}K`;
      if (count < 1000000) return `${Math.round(count / 1000)}K`;
      if (count < 10000000) return `${(count / 1000000).toFixed(1).replace(/\.0$/, "")}M`;
      return `${Math.round(count / 1000000)}M`;
    };
    const composer = $("#reel-create-dialog");
    const loading = $("[data-reels-loading]", reels), loadError = $("[data-reels-error]", reels);
    const loadingBanner = $("[data-reels-loading-banner]");
    const endNotice = $("[data-reels-end]", reels), sentinel = $("[data-reels-sentinel]", reels);
    const refreshError = $("[data-reels-refresh-error]", reels);
    const currentUrl = new URL(location.href);
    if (currentUrl.searchParams.get("create") === "1" && composer) {
      composer.showModal();
      currentUrl.searchParams.delete("create");
      history.replaceState(null, "", currentUrl.pathname + currentUrl.search + currentUrl.hash);
    }
    const ICON = (n) => `<svg class="i"><use href="#i-${n}"/></svg>`;
    const vid = (r) => $("video", r);
    const arm = (r) => {                       // give a reel its source (lazy), metadata only
      const v = r && vid(r); if (!v) return;
      if (!v.getAttribute("src") && v.dataset.src) { v.preload = "metadata"; v.src = v.dataset.src; }
    };
    function nodesFromHtml(html) {
      const template = document.createElement("template");
      template.innerHTML = html;
      return [...template.content.querySelectorAll("[data-reel]")];
    }
    function prependReels(html, preservePosition = true) {
      const empty = $("[data-reels-empty]", reels); if (empty) empty.remove();
      const existing = new Set($$("[data-reel]", reels).map((r) => r.dataset.id));
      const nodes = nodesFromHtml(html).filter((r) => !existing.has(r.dataset.id));
      if (!nodes.length) return 0;
      const oldHeight = reels.scrollHeight, oldTop = reels.scrollTop, fragment = document.createDocumentFragment();
      nodes.forEach((r) => fragment.append(r));
      reels.insertBefore(fragment, reels.firstChild);
      if (preservePosition) reels.scrollTop = oldTop + reels.scrollHeight - oldHeight;
      watch();
      return nodes.length;
    }
    function appendReels(html) {
      const existing = new Set($$("[data-reel]", reels).map((r) => r.dataset.id));
      const nodes = nodesFromHtml(html).filter((r) => !existing.has(r.dataset.id));
      const fragment = document.createDocumentFragment();
      nodes.forEach((r) => fragment.append(r));
      reels.insertBefore(fragment, loading);
      watch();
      return nodes.length;
    }
    function syncFeedStatus() {
      loading.hidden = true;
      loadError.hidden = true;
      endNotice.hidden = Boolean(reels.dataset.cursor) || !$$("[data-reel]", reels).length;
    }
    const flash = (r, icon) => {
      const f = $(".reel-flash", r); if (!f) return;
      f.innerHTML = ICON(icon); f.classList.remove("go"); void f.offsetWidth; f.classList.add("go");
    };
    function playReel(r) {
      const v = vid(r); if (!v) return;
      arm(r); arm(r.nextElementSibling);       // start the next one buffering for a seamless swipe
      v.muted = muted; v.classList.remove("failed"); $(".reel-retry", r).hidden = true;
      const pr = v.play(); if (pr && pr.catch) pr.catch(() => { /* autoplay blocked until a tap */ });
    }
    function stopReelViewWatch(r) {
      if (r._viewWatchStarted) {
        r._viewWatchMs = (r._viewWatchMs || 0) + performance.now() - r._viewWatchStarted;
        r._viewWatchStarted = 0;
      }
    }
    function startReelViewWatch(r) {
      const v = vid(r);
      if (!v || !r._viewVisible || document.hidden || v.paused || r.dataset.authorReel === "true"
          || reportedReelViews.has(r.dataset.id) || r._viewWatchStarted) return;
      r._viewWatchStarted = performance.now();
    }
    function reportReelView(r) {
      const reelId = r.dataset.id, viewUrl = r.dataset.viewUrl;
      if (!viewUrl || r.dataset.authorReel === "true" || reportedReelViews.has(reelId)) return;
      const watchedMs = (r._viewWatchMs || 0) + (r._viewWatchStarted ? performance.now() - r._viewWatchStarted : 0);
      if (watchedMs < VIEW_MIN_SECONDS * 1000) return;
      reportedReelViews.add(reelId);
      stopReelViewWatch(r);
      fetch(viewUrl, {
        method: "POST",
        credentials: "same-origin",
        keepalive: true,
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrf(), "X-Requested-With": "fetch" },
        body: JSON.stringify({ watched_seconds: watchedMs / 1000 })
      }).then((response) => response.ok ? response.json() : null).then((result) => {
        if (!result) return;
        const counter = $("[data-reel-view-count]", r);
        if (counter) counter.textContent = formatReelCount(result.views_count);
        const label = `${Math.max(0, Number(result.views_count) || 0).toLocaleString()} views`;
        const countLabel = $(".reel-view-count", r);
        if (countLabel) { countLabel.setAttribute("aria-label", label); countLabel.title = label; }
      }).catch(() => { /* view tracking must never interrupt playback */ });
    }
    function syncReelViewTimer(r) {
      const v = vid(r);
      if (!v) return;
      if (r._viewVisible && !document.hidden && !v.paused) startReelViewWatch(r);
      else stopReelViewWatch(r);
    }
    // how much of a reel was actually watched (1 = whole reel, >1 = replayed); fast swipe-aways count as "not for me"
    function reportWatch(r) {
      const v = vid(r); const ms = (r._ms || 0) + (r._t0 ? performance.now() - r._t0 : 0); r._ms = 0; r._t0 = 0; r._active = false;
      if (!v || !v.duration || !isFinite(v.duration) || ms < 400) return;
      pushSignal(r.dataset.id, "watch", +(ms / (v.duration * 1000)).toFixed(2));
    }
    const io = new IntersectionObserver((entries) => {
      entries.forEach((en) => {
        const r = en.target, v = vid(r); if (!v) return;
        if (en.intersectionRatio > 0.1) arm(r);
        r._viewVisible = en.isIntersecting && en.intersectionRatio >= 0.6;
        syncReelViewTimer(r);
        if (en.isIntersecting && en.intersectionRatio > 0.65) { $$("video", reels).forEach((o) => { if (o !== v) o.pause(); }); r._active = true; playReel(r); document.dispatchEvent(new CustomEvent("cirq:reel-active", { detail: { id: r.dataset.id } })); }
        else { if (r._active) reportWatch(r); v.pause(); if (en.intersectionRatio === 0) v.currentTime = 0; }
      });
    }, { root: reels, threshold: [0, 0.1, 0.65] });
    const moreIO = "IntersectionObserver" in window ? new IntersectionObserver((entries) => {
      if (entries.some((en) => en.isIntersecting)) loadMoreReels();
    }, { root: reels, rootMargin: "600px 0px", threshold: 0 }) : null;

    function wire(r) {                         // per-reel video events (progress, buffering, errors)
      const v = vid(r); if (!v || v._wired) return; v._wired = 1;
      const bar = $(".reel-progress i", r);
      const toggle = $("[data-reel-toggle]", r);
      v.addEventListener("playing", () => { r.classList.add("playing"); toggle.setAttribute("aria-label", "Pause reel"); if (!r._t0) r._t0 = performance.now(); syncReelViewTimer(r); });
      v.addEventListener("pause", () => { r.classList.remove("playing"); toggle.setAttribute("aria-label", "Play reel"); if (r._t0) { r._ms = (r._ms || 0) + performance.now() - r._t0; r._t0 = 0; } stopReelViewWatch(r); });
      v.addEventListener("timeupdate", () => {
        if (v.duration) bar.style.transform = `scaleX(${v.currentTime / v.duration})`;
        reportReelView(r);
      });
      ["waiting", "loadstart", "seeking"].forEach((ev) => v.addEventListener(ev, () => {
        r.classList.add("buffering");
        if (ev !== "loadstart") stopReelViewWatch(r);
      }));
      ["playing", "canplay", "loadeddata"].forEach((ev) => v.addEventListener(ev, () => { r.classList.remove("buffering"); if (ev === "canplay" || ev === "loadeddata") syncReelViewTimer(r); }));
      v.addEventListener("error", () => { r.classList.remove("buffering"); if (v.getAttribute("src")) { v.classList.add("failed"); $(".reel-retry", r).hidden = false; } });
    }
    const watch = () => {
      $$("[data-reel]", reels).forEach((r) => { if (!r._w) { r._w = 1; wire(r); io.observe(r); } });
      if (moreIO && sentinel) moreIO.observe(sentinel);
      syncFeedStatus();
    };
    watch();
    document.addEventListener("visibilitychange", () => {
      $$("[data-reel]", reels).forEach((r) => {
        const v = vid(r);
        if (document.hidden) stopReelViewWatch(r);
        else if (v && !v.paused) startReelViewWatch(r);
      });
    });
    document.addEventListener("cirq:reel-posted", (e) => {
      prependReels(e.detail.html, false);
      if (e.detail.newest) reels.dataset.newest = e.detail.newest;
      syncFeedStatus();
      reels.scrollTo({ top: 0, behavior: "smooth" });
    });

    // tap = play/pause, double-tap = like (with a heart burst), like Instagram
    let lastTap = 0, tapTimer = null;
    function burst(r, x, y) {
      const h = document.createElement("div"); h.className = "reel-burst"; h.innerHTML = ICON("heart");
      const box = r.getBoundingClientRect(); h.style.left = (x - box.left) + "px"; h.style.top = (y - box.top) + "px";
      r.append(h); setTimeout(() => h.remove(), 800);
    }
    reels.addEventListener("click", (e) => {
      if (e.target.closest("[data-reels-retry]")) { loadMoreReels(true); return; }
      if (e.target.closest("[data-reels-refresh-retry]")) { refreshNewReels(true); return; }
      const retry = e.target.closest("[data-reel-retry]");
      if (retry) { const r = retry.closest("[data-reel]"), v = vid(r); const src = v.dataset.src; v.removeAttribute("src"); v.load(); v.src = src; playReel(r); return; }
      const mu = e.target.closest("[data-reel-mute]");
      if (mu) {
        muted = !muted; $$("video", reels).forEach((v) => (v.muted = muted));
        $$("[data-reel-mute]", reels).forEach((b) => { b.innerHTML = ICON(muted ? "mute" : "volume"); b.setAttribute("aria-label", muted ? "Unmute" : "Mute"); });
        return;
      }
      const tg = e.target.closest("[data-reel-toggle]"); if (!tg) return;
      const r = tg.closest("[data-reel]"), v = vid(r), now = Date.now();
      if (now - lastTap < 300) {               // double tap
        clearTimeout(tapTimer); lastTap = 0;
        const like = $("[data-like]", r); if (like && !like.classList.contains("liked")) like.click();
        burst(r, e.clientX, e.clientY); return;
      }
      lastTap = now;
      tapTimer = setTimeout(() => {            // single tap (after we know it is not a double tap)
        if (v.paused) { if (muted) { /* stay muted until the user chooses */ } v.play().catch(() => {}); flash(r, "play"); }
        else { v.pause(); flash(r, "pause"); }
      }, 260);
    });

    document.addEventListener("visibilitychange", () => { if (document.hidden) { $$("[data-reel]", reels).forEach((r) => { if (r._active) reportWatch(r); }); $$("video", reels).forEach((v) => v.pause()); } });
    document.addEventListener("keydown", (e) => {   // desktop: arrows move between reels, space pauses
      if (/INPUT|TEXTAREA|SELECT/.test((e.target.tagName || ""))) return;
      const rs = $$("[data-reel]", reels); const cur = rs.findIndex((r) => { const b = r.getBoundingClientRect(), c = reels.getBoundingClientRect(); return Math.abs(b.top - c.top) < c.height / 2; });
      if ((e.key === "ArrowDown" || e.key === "j") && rs.length) { e.preventDefault(); rs[Math.min(rs.length - 1, cur + 1)].scrollIntoView({ behavior: "smooth" }); }
      if ((e.key === "ArrowUp" || e.key === "k") && rs.length) { e.preventDefault(); rs[Math.max(0, cur - 1)].scrollIntoView({ behavior: "smooth" }); }
      if (e.key === " " && cur >= 0) { const v = vid(rs[cur]); if (v) { e.preventDefault(); v.paused ? v.play().catch(() => {}) : v.pause(); } }
      if (e.key === "m") { const b = $("[data-reel-mute]", reels); b && b.click(); }
    });

    let busy = false;
    let loadFailed = false;
    async function loadMoreReels(retry = false) {
      const cur = reels.dataset.cursor; if (!cur || busy || (loadFailed && !retry)) return;
      busy = true; loadFailed = false; loading.hidden = false; loadingBanner.hidden = false; loadError.hidden = true; endNotice.hidden = true;
      try {
        const res = await fetch(`${reels.dataset.moreUrl}?before=${encodeURIComponent(cur)}&partial=1`, { credentials: "same-origin" });
        if (!res.ok) throw new Error(`Reel request failed (${res.status}).`);
        const html = await res.text();
        reels.dataset.cursor = res.headers.get("X-Next-Cursor") || "";
        appendReels(html);
        loading.hidden = true; loadingBanner.hidden = true;
        syncFeedStatus();
        if (reels.dataset.cursor && sentinel) {
          const box = reels.getBoundingClientRect(), end = sentinel.getBoundingClientRect();
          if (end.top < box.bottom + 600) requestAnimationFrame(() => loadMoreReels());
        }
      } catch (err) {
        loadFailed = true; loadError.hidden = false; loading.hidden = true; loadingBanner.hidden = true;
        const message = $("span", loadError); if (message) message.textContent = `Couldn't load more reels. ${err.message}`;
      } finally { busy = false; }
    }
    let refreshBusy = false;
    async function refreshNewReels(retry = false) {
      if (refreshBusy || busy || (document.hidden && !retry)) return;
      refreshBusy = true; if (refreshError) refreshError.hidden = true;
      try {
        for (let page = 0; page < 5; page++) {
          const marker = reels.dataset.newest;
          const query = marker ? `?partial=1&after=${encodeURIComponent(marker)}` : "?partial=1";
          const res = await fetch(`${reels.dataset.moreUrl}${query}`, { credentials: "same-origin" });
          if (!res.ok) throw new Error(`Refresh failed (${res.status}).`);
          const html = await res.text();
          if (marker) {
            const next = res.headers.get("X-After-Cursor");
            if (next) reels.dataset.newest = next;
            prependReels(html);
            if (res.headers.get("X-More-Newer") !== "1") break;
          } else {
            prependReels(html);
            const newest = res.headers.get("X-Newest-Cursor");
            if (newest) reels.dataset.newest = newest;
            if (!reels.dataset.cursor) reels.dataset.cursor = res.headers.get("X-Next-Cursor") || "";
            break;
          }
        }
        syncFeedStatus();
      } catch (err) {
        if (refreshError) {
          refreshError.hidden = false;
          const message = $("span", refreshError); if (message) message.textContent = `Couldn't check for new reels. ${err.message}`;
        }
      } finally { refreshBusy = false; }
    }
    if (moreIO && sentinel) moreIO.observe(sentinel);
    else reels.addEventListener("scroll", () => {
      if (sentinel && sentinel.getBoundingClientRect().top < reels.getBoundingClientRect().bottom + 600) loadMoreReels();
    }, { passive: true });
    reels.addEventListener("click", (e) => {
      if (e.target.closest("[data-reels-refresh-retry]")) refreshNewReels(true);
    });
    window.setInterval(() => refreshNewReels(), 30000);
  }

  /* ---------- reel composer: preview, validation, real upload progress ---------- */
  const rform = $("[data-reel-form]");
  if (rform) {
    const q = (s) => $(s, rform);
    const file = q("#rc-file"), pick = q("[data-rc-pick]"), edit = q("[data-rc-edit]"), video = q("[data-rc-video]");
    const err = q("[data-rc-error]"), overlay = q("[data-rc-overlay]"), submit = q("[data-rc-submit]");
    const ring = q("[data-rc-ring]"), pct = q("[data-rc-pct]"), status = q("[data-rc-status]");
    const maxBytes = (+rform.dataset.maxMb || 60) * 1024 * 1024, maxSec = +rform.dataset.maxSec || 90;
    const CIRC = 2 * Math.PI * 19; ring.style.strokeDasharray = CIRC;
    let xhr = null, url = null, duration = 0;
    const mb = (n) => (n / 1048576).toFixed(n > 10485760 ? 0 : 1) + " MB";
    const fmt = (t) => `${Math.floor(t / 60)}:${String(Math.round(t % 60)).padStart(2, "0")}`;
    const showErr = (m) => { err.textContent = m; err.hidden = !m; };
    const setProgress = (p) => { ring.style.strokeDashoffset = CIRC * (1 - p); pct.textContent = Math.round(p * 100) + "%"; };
    function reset() {
      if (url) URL.revokeObjectURL(url); url = null; duration = 0; file.value = ""; video.removeAttribute("src"); video.load();
      edit.hidden = true; pick.hidden = false; overlay.hidden = true; showErr(""); submit.disabled = false; setProgress(0);
    }
    file.addEventListener("change", () => {
      const f = file.files[0]; if (!f) return; showErr("");
      const okType = /^video\//.test(f.type) || /\.(mp4|mov|m4v|webm)$/i.test(f.name);
      if (!okType) { reset(); toast("Please choose a video file.", "error"); return; }
      if (f.size > maxBytes) { reset(); toast(`That video is ${mb(f.size)}. The limit is ${mb(maxBytes)}.`, "error"); return; }
      pick.hidden = true; edit.hidden = false; q("[data-rc-name]").textContent = f.name; q("[data-rc-info]").textContent = mb(f.size);
      q("[data-rc-nopreview]").hidden = true;
      url = URL.createObjectURL(f); video.src = url;
      video.onloadedmetadata = () => {
        duration = video.duration || 0;
        q("[data-rc-info]").textContent = `${mb(f.size)} · ${duration ? fmt(duration) : ""}`;
        if (duration > maxSec + 1) showErr(`This video is ${fmt(duration)} long. Reels can be up to ${maxSec} seconds. Trim it and try again.`);
        video.play().catch(() => {});
      };
      video.onerror = () => { q("[data-rc-nopreview]").hidden = false; };   // e.g. an HEVC .mov the browser can't preview
    });
    q("[data-rc-remove]").addEventListener("click", reset);
    q("[data-rc-caption]").addEventListener("input", (e) => { q("[data-rc-count]").textContent = e.target.value.length; });
    video.addEventListener("click", () => (video.paused ? video.play() : video.pause()));
    q("[data-rc-cancel]").addEventListener("click", () => { if (xhr) xhr.abort(); });

    function fail(m) { overlay.hidden = true; submit.disabled = false; showErr(m); xhr = null; window.removeEventListener("beforeunload", warn); }
    const warn = (e) => { e.preventDefault(); e.returnValue = ""; };
    rform.addEventListener("submit", (e) => {
      e.preventDefault();
      const f = file.files[0]; if (!f) { showErr("Select a video first."); return; }
      if (duration > maxSec + 1) { showErr(`Reels can be up to ${maxSec} seconds.`); return; }
      showErr(""); submit.disabled = true; overlay.hidden = false; status.textContent = "Uploading…"; setProgress(0);
      xhr = new XMLHttpRequest(); xhr.open("POST", rform.action); xhr.responseType = "json"; xhr.timeout = 240000;
      xhr.setRequestHeader("X-CSRFToken", csrf()); xhr.setRequestHeader("X-Requested-With", "fetch");
      xhr.upload.onprogress = (ev) => { if (!ev.lengthComputable) return; const p = ev.loaded / ev.total; setProgress(p * 0.98); if (p >= 1) status.textContent = "Processing your reel…"; };
      xhr.onload = () => {
        const r = xhr.response || {};
        if (xhr.status >= 200 && xhr.status < 300 && r.ok) {
          setProgress(1); status.textContent = "Posted!"; window.removeEventListener("beforeunload", warn);
          if (r.html && $("#reels")) {
            document.dispatchEvent(new CustomEvent("cirq:reel-posted", { detail: { html: r.html, newest: r.newest } }));
            const dialog = rform.closest("dialog"); if (dialog && dialog.open) dialog.close();
            reset(); toast("Your reel is live.", "success"); return;
          }
          location.href = r.redirect || "/reels"; return;
        }
        const byStatus = { 413: "That video is larger than CIRQ allows.", 429: "You've posted a lot of reels. Please wait a while and try again.", 502: "The server was busy. Please try again.", 504: "The upload took too long. Try a shorter or smaller video." };
        fail(r.error || byStatus[xhr.status] || "Upload failed. Please try again.");
      };
      xhr.onerror = () => fail("Connection lost. Check your internet and tap Share reel to try again.");
      xhr.ontimeout = () => fail("The upload took too long. Try a shorter video or a better connection.");
      xhr.onabort = () => fail("");
      window.addEventListener("beforeunload", warn);
      xhr.send(new FormData(rform));
    });
  }

  /* ---------- chat ---------- */
  const chat = $(".chat-wrap");
  if (chat) {
    const box = $("#bubbles"), form = $("#chat-form");
    const bottom = () => (box.scrollTop = box.scrollHeight); bottom();
    let last = chat.dataset.last || "";
    const keepLast = () => { const m = $$(".msg", box).pop(); if (m) last = m.dataset.ts; };
    const formInput = form && $("#chat-file-input", form);
    const previews = $("#chat-file-previews"), status = $("#chat-upload-status");
    const progress = $("#chat-upload-progress"), statusLabel = $("#chat-upload-label");
    const cancelUpload = $("#chat-upload-cancel"), retryUpload = $("#chat-upload-retry");
    const maxFiles = Number($(".chat-composer")?.dataset.maxFiles || 10);
    const selected = [];
    let uploadRequest = null, messageRequestId = null, retryLocked = false;
    const prettySize = (bytes) => bytes >= 1048576 ? `${(bytes / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`;
    const newMessageId = () => "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
      const r = Math.random() * 16 | 0;
      return (c === "x" ? r : (r & 3 | 8)).toString(16);
    });
    function renderPreviews() {
      previews.replaceChildren();
      previews.hidden = !selected.length;
      selected.forEach((item, index) => {
        const card = document.createElement("div"); card.className = "chat-file-preview";
        const file = item.file, mime = file.type || "", extension = file.name.includes(".") ? file.name.split(".").pop().toUpperCase() : "FILE";
        if (mime.startsWith("image/") || mime.startsWith("video/")) {
          const media = document.createElement(mime.startsWith("video/") ? "video" : "img");
          media.src = item.url; media.alt = mime.startsWith("video/") ? "" : file.name;
          if (media.tagName === "VIDEO") { media.muted = true; media.preload = "metadata"; }
          card.append(media);
        } else {
          const icon = document.createElement("span"); icon.className = "chat-file-type"; icon.textContent = extension;
          card.append(icon);
        }
        const detail = document.createElement("span"); detail.className = "chat-file-preview-text";
        const name = document.createElement("b"); name.textContent = file.name;
        const size = document.createElement("small"); size.textContent = prettySize(file.size);
        detail.append(name, size); card.append(detail);
        const remove = document.createElement("button"); remove.type = "button"; remove.className = "chat-file-preview-x";
        remove.textContent = "×"; remove.setAttribute("aria-label", `Remove ${file.name}`);
        remove.disabled = retryLocked || Boolean(uploadRequest);
        remove.addEventListener("click", () => {
          if (retryLocked || uploadRequest) return;
          URL.revokeObjectURL(item.url); selected.splice(index, 1); messageRequestId = null; renderPreviews();
        });
        card.append(remove); previews.append(card);
      });
    }
    function addFiles(files) {
      if (!form || retryLocked || uploadRequest) return;
      const incoming = [...files];
      if (selected.length + incoming.length > maxFiles) { toast(`You can attach up to ${maxFiles} files per message.`, "error"); return; }
      incoming.forEach((file) => selected.push({ file, url: URL.createObjectURL(file) }));
      renderPreviews();
    }
    function lockComposer(locked) {
      if (!form) return;
      form.elements.namedItem("body").disabled = locked;
      formInput.disabled = locked;
      $(".send", form).disabled = locked;
      $$(".chat-file-preview-x", previews).forEach((button) => { button.disabled = locked; });
    }
    function showFailure(message, canRetry) {
      uploadRequest = null; status.hidden = false; statusLabel.textContent = message;
      cancelUpload.hidden = true; retryUpload.hidden = !canRetry; progress.value = 0;
      retryLocked = canRetry; lockComposer(canRetry);
      if (!canRetry) messageRequestId = null;
      toast(message, "error");
    }
    function clearSelected() {
      selected.forEach((item) => URL.revokeObjectURL(item.url));
      selected.length = 0; renderPreviews();
    }
    function sendMessage() {
      if (!form || uploadRequest || retryLocked) return;
      const body = form.elements.namedItem("body").value;
      if (!body.trim() && !selected.length) return;
      const payload = new FormData(form);
      const hasAttachments = selected.length > 0;
      if (!hasAttachments) {
        const button = $(".send", form); button.disabled = true;
        api(form.action, { body: payload }).then((result) => {
          const empty = $("p.muted", box); empty && empty.remove();
          box.insertAdjacentHTML("beforeend", result.html); last = result.ts; bottom();
          form.elements.namedItem("body").value = ""; form.elements.namedItem("body").focus();
        }).catch((error) => toast(error.message, "error")).finally(() => { button.disabled = false; });
        return;
      }
      messageRequestId ||= newMessageId();
      payload.set("message_id", messageRequestId);
      selected.forEach((item) => payload.append("attachments", item.file, item.file.name));
      const xhr = new XMLHttpRequest();
      uploadRequest = xhr; status.hidden = false; retryUpload.hidden = true; cancelUpload.hidden = false;
      statusLabel.textContent = "Uploading…"; progress.value = 0;
      lockComposer(true);
      xhr.open("POST", form.dataset.attachmentAction);
      xhr.setRequestHeader("X-CSRFToken", csrf() || (form.querySelector('input[name="csrf_token"]') || {}).value || "");
      xhr.setRequestHeader("X-Requested-With", "fetch");
      xhr.timeout = 170000;
      xhr.upload.onprogress = (event) => {
        if (event.lengthComputable) { progress.value = Math.round(event.loaded / event.total * 100); statusLabel.textContent = `Uploading… ${progress.value}%`; }
      };
      xhr.onload = () => {
        let result = xhr.response;
        if (!result || typeof result !== "object") {
          try { result = JSON.parse(xhr.responseText); } catch (_) { result = {}; }
        }
        if (xhr.status < 200 || xhr.status >= 300 || !result.html) {
          const message = result.error || "Upload failed. Please try again.";
          showFailure(message, xhr.status === 0 || xhr.status === 429 || xhr.status >= 500);
          lockComposer(retryLocked); return;
        }
        uploadRequest = null; retryLocked = false; lockComposer(false);
        const empty = $("p.muted", box); empty && empty.remove();
        box.insertAdjacentHTML("beforeend", result.html); last = result.ts; messageRequestId = null;
        form.elements.namedItem("body").value = ""; clearSelected(); status.hidden = true; bottom(); watchChatMedia(box);
        form.elements.namedItem("body").focus();
      };
      xhr.onerror = () => showFailure("Connection lost. Your message wasn't confirmed; retry the upload.", true);
      xhr.ontimeout = () => showFailure("The upload timed out. Your files are still selected; retry when ready.", true);
      xhr.onabort = () => showFailure("Upload canceled. Your files are still selected; retry when ready.", true);
      xhr.send(payload);
    }
    function watchChatMedia(root) {
      $$("[data-lazy-src]", root).forEach((media) => {
        if (media._chatObserved) return;
        media._chatObserved = true;
        if (chatMediaObserver) chatMediaObserver.observe(media);
        else loadChatMedia(media);
      });
    }
    function loadChatMedia(media) {
      const src = media.dataset.lazySrc;
      if (!src) return;
      media.dataset.refreshSrc = src; media.src = src; delete media.dataset.lazySrc;
      if (media.tagName === "VIDEO") media.load();
    }
    function refreshChatMedia(media) {
      const src = media.dataset.refreshSrc;
      if (!src || media.dataset.refreshing === "1") return;
      media.dataset.refreshing = "1";
      const currentTime = media.tagName === "VIDEO" ? media.currentTime : 0;
      const retry = new URL(src, location.href); retry.searchParams.set("_refresh", Date.now());
      media.addEventListener("loadedmetadata", () => {
        if (currentTime > 0) media.currentTime = currentTime;
      }, { once: true });
      media.src = retry.href;
      if (media.tagName === "VIDEO" || media.tagName === "AUDIO") media.load();
    }
    const chatMediaObserver = "IntersectionObserver" in window ? new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        loadChatMedia(entry.target); chatMediaObserver.unobserve(entry.target);
      });
    }, { root: box, rootMargin: "160px" }) : null;
    watchChatMedia(box);
    box.addEventListener("error", (event) => {
      if (event.target.matches("img, video, audio")) refreshChatMedia(event.target);
    }, true);
    box.addEventListener("load", (event) => {
      if (event.target.tagName === "IMG") delete event.target.dataset.refreshing;
    }, true);
    box.addEventListener("loadeddata", (event) => {
      if (event.target.matches("video, audio")) delete event.target.dataset.refreshing;
    }, true);
    if (form) {
      form.addEventListener("submit", (event) => { event.preventDefault(); sendMessage(); });
      formInput.addEventListener("change", () => { addFiles(formInput.files); formInput.value = ""; });
      cancelUpload.addEventListener("click", () => { if (uploadRequest) uploadRequest.abort(); });
      retryUpload.addEventListener("click", () => { retryLocked = false; lockComposer(false); sendMessage(); });
      chat.addEventListener("dragover", (event) => {
        if (![...event.dataTransfer.types].includes("Files")) return;
        event.preventDefault(); chat.classList.add("dragging");
      });
      chat.addEventListener("dragleave", (event) => {
        if (!chat.contains(event.relatedTarget)) chat.classList.remove("dragging");
      });
      chat.addEventListener("drop", (event) => {
        if (!event.dataTransfer.files.length) return;
        event.preventDefault(); chat.classList.remove("dragging"); addFiles(event.dataTransfer.files);
      });
      document.addEventListener("paste", (event) => {
        if (!chat.contains(event.target) || retryLocked || uploadRequest) return;
        const images = [...(event.clipboardData?.items || [])]
          .filter((item) => item.kind === "file" && item.type.startsWith("image/"))
          .map((item) => item.getAsFile()).filter(Boolean)
          .map((file) => {
            if (file.name) return file;
            const subtype = file.type.split("/")[1] || "png";
            const extension = subtype === "jpeg" ? "jpg" : subtype === "heif" ? "heic" : subtype.split("+")[0];
            return new File([file], `clipboard-${Date.now()}.${extension}`, { type: file.type });
          });
        if (images.length) { event.preventDefault(); addFiles(images); }
      });
      box.addEventListener("click", async (event) => {
        const image = event.target.closest("[data-lightbox-src]");
        if (image) {
          const dialog = $("#chat-lightbox"), preview = $("#chat-lightbox-image");
          preview.src = image.dataset.lightboxSrc; preview.style.transform = "scale(1)";
          $("[data-lightbox-download]", dialog).href = image.dataset.downloadSrc;
          dialog.showModal(); return;
        }
        const pdf = event.target.closest("[data-pdf-preview-url]");
        if (pdf) {
          const dialog = $("#chat-pdf-modal");
          try {
            const result = await api(pdf.dataset.pdfPreviewUrl, { method: "GET" });
            $("#chat-pdf-title", dialog).textContent = pdf.dataset.title;
            $("#chat-pdf-frame", dialog).src = result.url;
            $("#chat-pdf-download", dialog).href = pdf.dataset.downloadSrc;
            dialog.showModal();
          } catch (error) { toast(error.message, "error"); }
        }
      });
      $("#chat-lightbox").addEventListener("click", (event) => {
        const zoom = event.target.closest("[data-zoom]");
        if (!zoom) return;
        const image = $("#chat-lightbox-image"), current = Number(image.dataset.zoom || 1);
        const next = Math.max(0.5, Math.min(4, current + (zoom.dataset.zoom === "in" ? 0.25 : -0.25)));
        image.dataset.zoom = String(next); image.style.transform = `scale(${next})`;
      });
      $("#chat-lightbox").addEventListener("close", () => {
        const image = $("#chat-lightbox-image"); image.removeAttribute("src"); image.dataset.zoom = "1";
      });
      $("#chat-pdf-modal").addEventListener("close", () => { $("#chat-pdf-frame").src = "about:blank"; });
    }
    setInterval(async () => {   // polling fallback (no websockets needed)
      if (document.hidden) return;
      try {
        const open = $$(".ticks:not(.read)", box).map((t) => t.dataset.tickId).slice(-60);      // my messages not yet seen
        const r = await api(`${chat.dataset.poll}?after=${encodeURIComponent(last)}${open.length ? `&ids=${open.join(",")}` : ""}`, { method: "GET" });
        if (r.html) { box.insertAdjacentHTML("beforeend", r.html); last = r.ts; bottom(); watchChatMedia(box); }
        const names = { sent: "Sent", delivered: "Delivered", read: "Seen" };
        Object.entries(r.ticks || {}).forEach(([id, state]) => {
          const t = box.querySelector(`.ticks[data-tick-id="${id}"]`);
          if (t && !t.classList.contains(state)) { t.className = `ticks ${state}`; t.title = names[state]; t.setAttribute("aria-label", names[state]); }
        });
      } catch (_) { /* transient */ }
    }, 4000);
    keepLast();

    /* disappearing messages: saved as soon as an option is picked */
    const dform = $("[data-disappear-form]"), dnote = $("[data-disappear-note]");
    if (dform) dform.addEventListener("change", async (event) => {
      if (event.target.name !== "seconds") return;
      try {
        const r = await api(dform.action, { body: new FormData(dform) });
        dnote.hidden = !r.seconds;
        $("[data-disappear-label]", dnote).textContent = r.label;
        toast(r.seconds ? `New messages will disappear for you after ${r.label}.` : "Disappearing messages are off.", "ok");
        dform.closest("dialog").close();
      } catch (error) { toast(error.message, "error"); }
    });
  }

  /* ---------- CIRQ FLIP (collectible student cards) ---------- */
  const flip = $("#flip");
  if (flip) {
    const deck = $("#flip-deck"), msg = $("#flip-msg"), sub = $("#flip-sub"), res = $("#flip-result"), startBtn = $("#flip-start"), stage = $("#stage");
    const mark = flip.dataset.mark; let busy = false, current = null;
    const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; };
    const short = (n) => (n >= 10000 ? Math.round(n / 1000) + "K" : n >= 1000 ? (n / 1000).toFixed(1) + "K" : String(n));
    const hearts = (left) => $$("#flip-hearts i").forEach((h, k) => h.classList.toggle("off", k >= left));
    function fillBack(card, p, extra) {   // all text is set with textContent, so student names can never inject markup
      const inner = $(".back .in", card); inner.textContent = "";
      card.classList.remove("r0", "r1", "r2", "r3", "r4", "r5", "r6", "dev");
      card.classList.add(p.developer ? "dev" : "r" + Math.min(p.tier_index, 6));
      const rating = el("div", "rating"); rating.append(el("b", "", p.developer ? "DEV" : short(p.followers)), el("small", "", p.developer ? "CIRQ" : "FOL")); inner.append(rating);
      if (p.developer) { const c = el("div", "crown"); c.innerHTML = '<svg class="i fill"><use href="#i-award"/></svg>'; inner.append(c); }
      const pt = el("div", "portrait");
      if (p.avatar) { const im = document.createElement("img"); im.src = p.avatar; im.alt = ""; pt.append(im); } else pt.textContent = (p.name || "?")[0];
      const np = el("div", "nameplate");
      np.append(el("b", "", p.name));
      if (p.username) np.append(el("small", "handle", "@" + p.username));
      if (p.school) np.append(el("small", "school", p.school));
      np.append(el("span", "tierbar", p.developer ? "The Developer" : p.tier));
      inner.append(pt, np, el("i", "shine")); if (extra) card.classList.add(extra);
    }
    function build(n) {
      deck.textContent = "";
      for (let i = 0; i < n; i++) {
        const b = el("button", "pcard r0"); b.type = "button"; b.dataset.card = i; b.disabled = true; b.setAttribute("aria-label", "Card " + (i + 1));
        const f = el("span", "face front"), fi = el("span", "in"), im = document.createElement("img"); im.src = mark; im.alt = ""; fi.append(im, el("span", "num", String(i + 1).padStart(2, "0")), el("i", "shine")); f.append(fi);
        const bk = el("span", "face back"); bk.append(el("span", "in")); const fl = el("span", "flipper"); fl.append(f, bk); b.append(fl); deck.append(b);
      }
    }
    function paint(s) {
      current = s;
      if (deck.children.length !== s.cards) build(s.cards);
      hearts(s.guesses_left);
      $$(".pcard", deck).forEach((c) => { c.classList.remove("flipped", "win", "reveal", "miss"); c.disabled = s.status !== "active"; });
      s.flipped.forEach((f) => { const c = deck.children[f.card]; fillBack(c, f.profile); c.classList.add("flipped", f.correct ? "win" : "miss"); c.disabled = true; });
      if (s.status === "active") {
        msg.textContent = "Choose the developer card"; sub.textContent = `${s.guesses_left} guess${s.guesses_left === 1 ? "" : "es"} left. Flip a card to see who's behind it.`;
        startBtn.hidden = true; res.hidden = true; return;
      }
      const dev = s.developer;
      if (s.status === "lost") { const c = deck.children[s.answer_index]; fillBack(c, dev); c.classList.add("flipped", "reveal"); }
      msg.textContent = s.status === "won" ? "You found the developer! 🎉" : "Out of guesses";
      sub.textContent = s.status === "won" ? `That was ${dev.name}.` : `${dev.name} was hiding behind card ${s.answer_index + 1}.`;
      startBtn.hidden = Boolean(s.reward && s.reward.claimable); startBtn.textContent = "Play again";
      const old = $("#mystery"); if (old) { const big = el("button", "pcard mystery flipped dev"); big.type = "button"; big.disabled = true; big.id = "mystery";
        const fr = el("span", "face front"); fr.append(el("span", "in")); const bk = el("span", "face back"); bk.append(el("span", "in")); const fl = el("span", "flipper"); fl.append(fr, bk); big.append(fl); fillBack(big, dev); old.replaceWith(big); }
    }
    function showWin(s) {
      res.hidden = false; res.className = "reward-pop"; res.textContent = "";
      res.append(el("p", "", `You spotted ${s.developer.name}!`));
      if (s.reward && s.reward.claimable) {
        const claimBtn = el("button", "btn block", `Claim ${s.reward.coins} app coins`);
        claimBtn.type = "button";
        claimBtn.addEventListener("click", async () => {
          claimBtn.disabled = true;
          try {
            const r = await api(flip.dataset.claim, { json: { round_id: s.round_id } });
            if (r.claimed) { location.reload(); return; }
            claimBtn.remove();
            res.append(el("p", "muted", r.note || "This reward is no longer available."));
          } catch (err) { toast(err.message, "error"); claimBtn.disabled = false; }
        });
        res.append(claimBtn);
      } else if (s.reward && s.reward.note) res.append(el("p", "muted", s.reward.note));
      if (s.developer.username) { const a = el("a", "link", "Visit " + s.developer.name + "'s profile"); a.href = "/u/" + s.developer.username; res.append(a); }
      res.scrollIntoView({ behavior: "smooth", block: "center" });
    }
    async function begin() {
      startBtn.classList.add("loading");
      try { const old = $("#mystery"); paint(await api(flip.dataset.start)); } catch (err) { toast(err.message, "error"); sub.textContent = err.message; }
      startBtn.classList.remove("loading");
    }
    startBtn.addEventListener("click", begin);
    deck.addEventListener("click", async (e) => {
      const c = e.target.closest(".pcard"); if (!c || c.disabled || busy) return; busy = true; c.classList.add("selected");
      try {
        const s = await api(flip.dataset.guess, { json: { card: +c.dataset.card } });
        c.classList.remove("selected"); paint(s);
        if (s.status === "won") showWin(s);
        else if (s.status === "lost") toast("No guesses left. Try another round!", "info");
      } catch (err) { c.classList.remove("selected"); toast(err.message, "error"); }
      busy = false;
    });
    api(flip.dataset.state, { method: "GET" }).then((s) => {
      if (s.active) paint(s);
      else if (s.pending_claim) { paint(s.pending_claim); showWin(s.pending_claim); }
    }).catch(() => {});
  }

  /* ---------- trivia ---------- */
  const tv = $("#trivia");
  if (tv) {
    const prompt = $("#t-prompt"), opts = $("#t-opts"), timer = $("#t-timer b"), pos = $("#t-pos"), pts = $("#t-pts"), note = $("#t-note");
    let raf, answered = false, q;
    async function load() {
      try { q = await api(tv.dataset.q, { method: "GET" }); } catch (err) { toast(err.message, "error"); return; }
      if (q.done) { location.href = q.redirect; return; }
      answered = false; note.textContent = ""; pos.textContent = `Question ${q.position} / ${q.total}`; pts.textContent = q.points;
      prompt.textContent = q.prompt; opts.innerHTML = "";
      q.options.forEach((o, i) => { const b = document.createElement("button"); b.type = "button"; b.className = "opt"; b.textContent = o; b.onclick = () => send(i); opts.append(b); });
      const total = q.time_limit * 1000, startAt = performance.now() - (total - q.remaining_ms);
      cancelAnimationFrame(raf);
      const tick = () => {
        const left = Math.max(0, total - (performance.now() - startAt));
        timer.style.transform = `scaleX(${left / total})`; timer.parentElement.classList.toggle("low", left < total * 0.25);
        if (left <= 0 && !answered) { send(null); return; }
        if (!answered) raf = requestAnimationFrame(tick);
      };
      tick();
    }
    async function send(choice) {
      if (answered) return; answered = true; cancelAnimationFrame(raf);
      $$(".opt", opts).forEach((b) => (b.disabled = true));
      try {
        const r = await api(tv.dataset.a, { json: { choice, position: q.position } });
        if (r.done) { location.href = r.redirect; return; }
        note.textContent = choice === null ? "Time's up!" : "Answer locked in."; setTimeout(load, 700);
      } catch (err) { toast(err.message, "error"); setTimeout(load, 900); }
    }
    load();
  }
})();
