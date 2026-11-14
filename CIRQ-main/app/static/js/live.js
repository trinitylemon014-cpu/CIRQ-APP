/* CIRQ live room. Video/audio flow browser <-> LiveKit directly; this page talks to Flask only for tokens, chat, gifts and control.
   All user text is inserted with textContent. No inline scripts, no secrets: the only credential here is a short-lived room token. */
(() => {
  "use strict";
  const root = document.getElementById("lr");
  if (!root) return;
  const SID = root.dataset.sid, IS_HOST = root.dataset.isHost === "1", API = `/live/api/${SID}`;
  const $ = (id) => document.getElementById(id);
  const csrf = () => (document.querySelector('meta[name="csrf-token"]') || {}).content || "";
  const el = (t, c, x) => { const e = document.createElement(t); if (c) e.className = c; if (x != null) e.textContent = x; return e; };
  const LK = () => window.LivekitClient;

  let S = null;                 // last room state
  let cursor = 0, pollTick = 0, pollTimer = null, ended = false, offset = 0;
  const seenEvents = new Set(), seenGifts = new Set();
  let room = null, oppRoom = null, myRole = "viewer", connecting = false, oppKey = "";
  let refetchT = null;

  // ---------------------------------------------------------------- helpers
  async function api(path, body, method) {
    const opt = { method: method || (body === undefined ? "GET" : "POST"), credentials: "same-origin", headers: { "X-CSRFToken": csrf(), "X-Requested-With": "fetch" } };
    if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
    let r;
    try { r = await fetch(path.startsWith("/") ? path : API + path, opt); } catch (e) { return { ok: false, error: "No connection. Check your internet.", network: true }; }
    const d = await r.json().catch(() => ({}));
    if (r.status === 401) { location.href = "/login"; return { ok: false }; }
    return { status: r.status, ...d, ok: r.ok && d.ok !== false };
  }
  let toastT = null;
  function toast(msg) {
    document.querySelectorAll(".lr-toast").forEach((x) => x.remove());
    const t = el("div", "lr-toast", msg); t.setAttribute("role", "status"); document.body.appendChild(t);
    clearTimeout(toastT); toastT = setTimeout(() => t.remove(), 4000);
  }
  const sheet = $("lr-sheet");
  function openSheet(title, node) {
    $("lr-sheet-title").textContent = title; $("lr-sheet-body").replaceChildren(node);
    if (!sheet.open) sheet.showModal();
  }
  const closeSheet = () => { if (sheet.open) sheet.close(); };
  $("lr-sheet-x").addEventListener("click", closeSheet);
  sheet.addEventListener("click", (e) => { if (e.target === sheet) closeSheet(); });
  function confirmBox(title, text, yes, onYes) {
    const b = el("div"); b.appendChild(el("p", "", text));
    const row = el("div", "lr-actions"), y = el("button", "btn danger", yes), n = el("button", "btn ghost", "Cancel");
    y.type = n.type = "button"; n.addEventListener("click", closeSheet); y.addEventListener("click", () => { closeSheet(); onYes(); });
    row.append(y, n); b.appendChild(row); openSheet(title, b);
  }
  function avatar(p, cls) {
    const a = el("span", cls || "lv-av");
    if (p && p.avatar) { const i = el("img"); i.src = p.avatar; i.alt = ""; a.appendChild(i); } else a.textContent = ((p && (p.username || p.display_name)) || "?")[0].toUpperCase();
    return a;
  }

  // ---------------------------------------------------------------- chat
  const chat = $("lr-chat");
  let stick = true;
  chat.addEventListener("scroll", () => { stick = chat.scrollHeight - chat.scrollTop - chat.clientHeight < 40; if (stick) $("lr-newmsg").style.display = "none"; });
  $("lr-newmsg").addEventListener("click", () => { chat.scrollTop = chat.scrollHeight; });
  function addLine(node) {
    chat.appendChild(node);
    while (chat.children.length > 200) chat.firstChild.remove();
    if (stick) chat.scrollTop = chat.scrollHeight; else $("lr-newmsg").style.display = "block";
  }
  function badges(u) { const f = document.createDocumentFragment(); ((u && u.badges) || []).forEach((b) => f.appendChild(el("span", "lr-b " + b, { host: "HOST", mod: "MOD", verified: "✓", bronze: "Bronze", silver: "Silver", gold: "Gold", diamond: "Diamond" }[b] || b))); return f; }
  function chatLine(ev) {
    const d = el("div", "lr-msg"); d.dataset.event = ev.id;
    const u = el("button", "u", ev.user ? ev.user.display_name : "someone"); u.type = "button";
    if (ev.user) u.addEventListener("click", () => userSheet(ev.user, ev));
    d.append(badges(ev.user), u, document.createTextNode(": "), document.createTextNode(ev.body || ""));
    return d;
  }
  function giftLine(ev) {
    const p = ev.payload || {}, d = el("div", "lr-msg gift"); d.dataset.event = ev.id;
    const who = (ev.user && ev.user.display_name) || p.sender_name || "Someone";
    let t = `${who} sent ${p.name || "a gift"}`;
    if (p.diamonds) t += ` (${p.diamonds} diamonds shown)`;
    d.textContent = "🎁 " + t; return d;
  }
  function sysLine(text) { const d = el("div", "lr-msg sys", text); addLine(d); }
  function reactFx(emoji) {
    const box = $("lr-reacts"); if (box.children.length > 14) return;
    const r = el("span", "lr-react", emoji); r.style.left = Math.random() * 30 + "px"; box.appendChild(r); setTimeout(() => r.remove(), 2500);
  }
  function setPinned(ev) {
    const box = $("lr-pinned"); box.classList.toggle("on", !!ev);
    if (ev) $("lr-pintext").textContent = (ev.payload && ev.payload.sender_name ? ev.payload.sender_name + ": " : "") + (ev.body || "");
    $("lr-unpin").hidden = !(ev && S && S.me.can_moderate);
  }
  $("lr-unpin").addEventListener("click", async () => { const r = await api("/unpin", {}); if (!r.ok) toast(r.error || "Couldn't unpin."); });

  let lastSent = 0;
  $("lr-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const inp = $("lr-input"), text = inp.value.trim();
    if (!text || ended) return;
    if (S && S.me.muted) { toast("You're muted in this live."); return; }
    if (S && !S.session.chat_enabled && !IS_HOST) { toast("Chat is turned off."); return; }
    if (Date.now() - lastSent < 700) return; lastSent = Date.now();
    inp.value = "";
    const r = await api("/chat", { body: text });
    if (!r.ok) { inp.value = text; toast(r.error || "Couldn't send."); } else poll(false);
  });
  $("lr-react").addEventListener("click", () => {
    const list = (S && S.reactions) || ["❤️"]; const b = el("div", "lr-actions");
    list.forEach((em) => { const x = el("button", "lr-ib", em); x.type = "button"; x.style.fontSize = "1.5rem"; x.addEventListener("click", async () => { closeSheet(); reactFx(em); const r = await api("/react", { emoji: em }); if (!r.ok) toast(r.error || "Slow down a little."); }); b.appendChild(x); });
    openSheet("React", b);
  });

  // ---------------------------------------------------------------- events
  function handle(ev, replay) {
    if (seenEvents.has(ev.id)) return; seenEvents.add(ev.id);
    const p = ev.payload || {};
    switch (ev.kind) {
      case "chat": addLine(chatLine(ev)); break;
      case "gift": addLine(giftLine(ev));
        if (!replay && !seenGifts.has(p.txn_id)) { seenGifts.add(p.txn_id); window.LiveGifts.play({ ...p, sender_name: p.sender_name || (ev.user && ev.user.display_name) }); }
        else if (p.txn_id) seenGifts.add(p.txn_id);
        break;
      case "reaction": if (!replay) reactFx(p.emoji || "❤️"); break;
      case "pin": setPinned(ev); break;
      case "unpin": setPinned(null); break;
      case "delete_msg": { const n = chat.querySelector(`[data-event="${p.event_id}"]`); if (n) n.remove(); break; }
      case "mod": if (!replay) { if (S && p.target === S.me.username) { refetch(); toast({ mute: "A moderator muted you.", unmute: "You can chat again.", timeout: "You've been timed out for a bit.", remove: "You were removed." }[p.action] || "Your status changed."); } } break;
      case "guest": if (!replay) { if (p.state === "accepted") sysLine(`${p.username} joined the stage.`); refetch(); } break;
      case "state": case "battle_invite": if (!replay) refetch(); break;
      case "battle_accept": case "battle_start": case "battle_end": if (!replay) refetch(); break;
      case "ended": if (!replay) showEnded(p.reason); break;
      default: break;
    }
  }

  // ---------------------------------------------------------------- polling
  async function poll(hb) {
    if (ended) return;
    const q = `?since=${cursor}` + (hb ? "&hb=1" : "") + (S && S.battle ? `&battle=${S.battle.id}` : "");
    const r = await api(`/poll${q}`);
    if (r.network) return;
    if (!r.ok) {
      if (r.status === 403 || r.status === 404) { showEnded(null, r.error || "You can no longer watch this live."); }
      else if (r.status === 429) { /* rate limited: next tick */ }
      return;
    }
    if (r.server_time) offset = Date.parse(r.server_time) - Date.now();
    (r.events || []).forEach((ev) => handle(ev, false));
    if (r.cursor > cursor) cursor = r.cursor;
    if (typeof r.viewer_count === "number") setCount(r.viewer_count);
    if (r.me && S) { S.me.balance = r.me.balance; S.me.muted = r.me.muted; renderBalance(); }
    if (r.battle) { S.battle = r.battle; renderBattle(); }
    if (r.status === "ended") showEnded();
  }
  function loop() {
    clearTimeout(pollTimer);
    const ms = Math.max(1, (S && S.poll_seconds) || 3) * 1000 * (document.hidden ? 3 : 1);
    pollTimer = setTimeout(async () => { pollTick++; await poll(pollTick % 4 === 0); loop(); }, ms);
  }
  document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(true); });
  function refetch() { clearTimeout(refetchT); refetchT = setTimeout(loadState, 250); }
  const setCount = (n) => { $("lr-count").textContent = `${n} watching`; };
  function renderBalance() { const b = $("lr-balance"); if (!IS_HOST && S) b.textContent = `${S.me.balance} coins`; }

  // ---------------------------------------------------------------- state
  async function loadState(first) {
    const r = await api("/state");
    if (!r.ok) { if (first) showEnded(null, r.error || "This live isn't available."); return; }
    S = r.state; applyState(first);
  }
  function applyState(first) {
    const s = S.session; ended = ended || s.status !== "live";
    $("lr-title").textContent = s.title;
    $("lr-sub").textContent = `@${S.host.username} · Lv ${S.host.level} · ${S.host.followers} followers`;
    const hav = $("lr-hav"); hav.replaceChildren(...avatar(S.host).childNodes);
    $("lr-phav").replaceChildren(...avatar(S.host).childNodes);
    setCount(S.viewer_count); renderBalance(); setPinned(S.pinned);
    const f = $("lr-follow");
    f.hidden = S.host.follow_state === "self"; f.textContent = { following: "Following", requested: "Requested" }[S.host.follow_state] || "Follow";
    f.classList.toggle("on", S.host.follow_state === "none" || !S.host.follow_state);
    const ag = $("lr-askguest"), lg = $("lr-leaveguest");
    if (ag) { const mine = S.guests.mine; ag.hidden = !(S.me.can_request_guest && !mine); lg.hidden = !(mine && mine.status === "accepted"); }
    $("lr-input").maxLength = S.max_chat || 200; $("lr-input").disabled = !S.session.chat_enabled && !IS_HOST;
    $("lr-input").placeholder = S.session.chat_enabled || IS_HOST ? "Say something…" : "Chat is off";
    if (first) {
      cursor = S.cursor || 0; offset = 0; S.history.forEach((ev) => { handle(ev, true); }); seenEvents.clear();
      S.history.forEach((e) => seenEvents.add(e.id)); setPinned(S.pinned);
      if (ended) showEnded(); else { connectMedia(); loop(); }
    } else {
      renderGuests(); renderBattle(); syncRole();
    }
    renderGuests(); renderBattle(); renderInvites();
    if (first) document.dispatchEvent(new Event("live:ready"));
  }

  // ---------------------------------------------------------------- media (LiveKit)
  function placeholder(text) { const ph = $("lr-ph"); ph.style.display = text ? "grid" : "none"; if (text) $("lr-phtxt").textContent = text; }
  function tileFor(container, key, name) {
    let t = container.querySelector(`[data-key="${key}"]`);
    if (!t) { t = el("div", "lr-tile"); t.dataset.key = key; if (name) t.appendChild(el("span", "nm", name)); container.appendChild(t); }
    return t;
  }
  function guestIdentities() { const m = {}; (S ? S.guests.accepted : []).forEach((g) => { if (g.identity) m[g.identity] = g; }); return m; }
  function attachTrack(track, pub, participant, rm) {
    const isOpp = rm === oppRoom;
    const gmap = isOpp ? {} : guestIdentities();
    const g = gmap[participant.identity];
    let tile;
    if (isOpp) tile = tileFor($("lr-opp") || $("lr-media"), "opp", "");
    else if (g) tile = tileFor($("lr-guests"), participant.identity, g.display_name);
    else tile = $("lr-main");
    if (track.kind === "video") {
      const v = track.attach(); v.setAttribute("playsinline", ""); v.dataset.tid = track.sid || pub.trackSid;
      if (pub.source === "screen_share") v.classList.add("ss"); else if (tile.querySelector("video.ss")) v.classList.add("pip");
      tile.querySelectorAll("video").forEach((o) => { if (o !== v && o.dataset.tid === v.dataset.tid) o.remove(); });
      tile.appendChild(v); if (tile === $("lr-main")) placeholder("");
      if (pub.source === "screen_share") tile.querySelectorAll("video:not(.ss)").forEach((x) => x.classList.add("pip"));
    } else if (track.kind === "audio") { const a = track.attach(); a.dataset.tid = pub.trackSid; a.style.display = "none"; document.body.appendChild(a); audioCheck(rm); }
  }
  function detachTrack(track) { track.detach().forEach((n) => { const t = n.parentElement; n.remove(); if (t && t.classList.contains("lr-tile") && t.id !== "lr-main" && !t.querySelector("video")) t.remove(); }); if (!$("lr-main").querySelector("video")) placeholder("Waiting for the host's video…"); }
  function audioCheck(rm) { const btn = $("lr-audio"); btn.hidden = !rm || rm.canPlaybackAudio; }
  $("lr-audio").addEventListener("click", async () => { try { if (room) await room.startAudio(); if (oppRoom) await oppRoom.startAudio(); } catch (e) {} $("lr-audio").hidden = true; });

  function wire(rm) {
    const E = LK().RoomEvent;
    rm.on(E.TrackSubscribed, (t, p, pa) => attachTrack(t, p, pa, rm));
    rm.on(E.TrackUnsubscribed, (t) => detachTrack(t));
    rm.on(E.AudioPlaybackStatusChanged, () => audioCheck(rm));
    rm.on(E.ParticipantDisconnected, () => { if (!$("lr-main").querySelector("video") && rm === room) placeholder("Waiting for the host's video…"); });
    rm.on(E.Disconnected, () => { if (rm === room && !ended && !connecting) { placeholder("Connection lost. Reconnecting…"); setTimeout(() => { if (!ended && room === rm) connectMedia(true); }, 2500); } });
    rm.on(E.LocalTrackUnpublished, () => {});
  }
  async function getToken(target) { return api("/token", { target }); }
  async function connectMedia(retry) {
    if (connecting || ended) return;
    if (!LK()) { placeholder("Video player isn't installed on this server. See LIVE_STREAMING.md (vendor the LiveKit client)."); return; }
    if (!S.provider_ready) { placeholder("Live video isn't connected on this server yet."); return; }
    connecting = true;
    try {
      if (room) { const old = room; room = null; try { old.disconnect(); } catch (e) {} }
      document.querySelectorAll("#lr-main video,#lr-guests .lr-tile").forEach((n) => n.remove());
      placeholder(retry ? "Reconnecting…" : "Connecting…");
      const t = await getToken("self");
      if (!t.ok) { placeholder(t.error || "Couldn't connect to the live video."); connecting = false; return; }
      myRole = t.role; const rm = new (LK().Room)({ adaptiveStream: true, dynacast: true });
      room = rm; wire(rm);
      await rm.connect(t.url, t.token);
      audioCheck(rm);
      rm.remoteParticipants.forEach((pa) => pa.trackPublications.forEach((pub) => { if (pub.isSubscribed && pub.track) attachTrack(pub.track, pub, pa, rm); }));
      if (t.can_publish) await publishLocal(rm, t.sources || []);
      else if (!$("lr-main").querySelector("video")) placeholder(S.session.mode === "audio" ? "Audio-only live. Turn your sound on." : "Waiting for the host's video…");
      if (S.session.mode === "audio" && !IS_HOST) { $("lr-ph").style.display = "grid"; $("lr-phtxt").textContent = "Audio-only live 🎙️"; }
    } catch (e) {
      placeholder("Couldn't connect to the live video. Check your connection.");
    } finally { connecting = false; }
  }
  async function publishLocal(rm, sources) {
    const lp = rm.localParticipant;
    try {
      if (sources.includes("camera")) { await lp.setCameraEnabled(true); lp.videoTrackPublications.forEach((pub) => { if (pub.track) attachLocal(pub.track, pub); }); }
      if (sources.includes("microphone")) await lp.setMicrophoneEnabled(true);
      addPublisherControls(sources);
    } catch (e) {
      const denied = e && /permission|denied|NotAllowed/i.test(e.name + e.message);
      toast(denied ? "Camera or microphone is blocked. Allow it in your browser's address bar, then reload." : "Couldn't start your camera or microphone.");
      if (IS_HOST) placeholder("Your camera/microphone couldn't start. Allow access and reload.");
    }
  }
  function attachLocal(track, pub) {
    const v = track.attach(); v.setAttribute("playsinline", ""); v.muted = true;
    let tile;
    if (IS_HOST) tile = $("lr-main"); else { const me = (S.guests.accepted || []).find((g) => g.username === S.me.username); tile = tileFor($("lr-guests"), (me && me.identity) || "me", "You"); }
    tile.querySelectorAll("video[data-local]").forEach((o) => o.remove()); v.dataset.local = "1";
    if (pub.source === "screen_share") v.classList.add("ss"); else if (tile.querySelector("video.ss")) v.classList.add("pip");
    tile.appendChild(v); if (IS_HOST) placeholder("");
  }
  function addPublisherControls(sources) {
    const old = $("lr-pubctl"); if (old) old.remove();
    const row = el("div", "lr-actions"); row.id = "lr-pubctl";
    const mk = (label, fn) => { const b = el("button", "btn sm ghost", label); b.type = "button"; b.addEventListener("click", () => fn(b)); row.appendChild(b); };
    const lp = room.localParticipant;
    if (sources.includes("microphone")) mk("Mute mic", async (b) => { const on = lp.isMicrophoneEnabled; await lp.setMicrophoneEnabled(!on); b.textContent = on ? "Unmute mic" : "Mute mic"; });
    if (sources.includes("camera")) mk("Camera off", async (b) => { const on = lp.isCameraEnabled; await lp.setCameraEnabled(!on); if (!on) lp.videoTrackPublications.forEach((p) => p.track && attachLocal(p.track, p)); b.textContent = on ? "Camera on" : "Camera off"; });
    if (IS_HOST && sources.includes("screen_share") && navigator.mediaDevices && navigator.mediaDevices.getDisplayMedia) mk("Share screen", async (b) => {
      try { const on = lp.isScreenShareEnabled; await lp.setScreenShareEnabled(!on); b.textContent = on ? "Share screen" : "Stop sharing"; lp.videoTrackPublications.forEach((p) => p.track && attachLocal(p.track, p)); }
      catch (e) { toast("Screen sharing wasn't started."); }
    });
    root.querySelector(".lr-side").appendChild(row);
  }
  // a viewer becomes (or stops being) a guest: reconnect with the right token
  function syncRole() {
    if (!room || connecting || ended) return;
    const mine = S.guests.mine, wantGuest = !!(mine && mine.status === "accepted");
    if (!IS_HOST && wantGuest !== (myRole === "guest")) connectMedia();
    else if (IS_HOST || wantGuest) { /* permission changes are applied by LiveKit live; ensure our own tiles are current */ }
  }

  // ---------------------------------------------------------------- PK battle
  let battleTimer = null;
  function renderBattle() {
    const b = S && S.battle, bar = $("lr-pk"), media = $("lr-media");
    if (!b || (b.status === "ended" && b.outcome === "cancelled") || b.status === "declined" || b.status === "invited" || b.status === "expired") {
      bar.classList.remove("on"); media.classList.remove("pk"); $("lr-pkresult").style.display = "none"; const o = $("lr-opp"); if (o) o.remove(); closeOpp(); clearInterval(battleTimer); return;
    }
    bar.classList.add("on");
    const a = b.sides.a, c = b.sides.b, sa = a ? a.score : 0, sb = c ? c.score : 0, tot = Math.max(1, sa + sb);
    $("lr-pka").style.width = Math.max(10, Math.round(sa * 100 / tot)) + "%"; $("lr-pka").textContent = sa; $("lr-pkb").textContent = sb;
    $("lr-pkna").textContent = a ? "@" + a.username : ""; $("lr-pknb").textContent = c ? "@" + c.username : "";
    const live = b.status === "countdown" || b.status === "active";
    if (live) {
      media.classList.add("pk");
      if (!$("lr-opp")) { const o = el("div", "lr-tile"); o.id = "lr-opp"; media.appendChild(o); }
      openOpp();
    } else { media.classList.remove("pk"); const o = $("lr-opp"); if (o) o.remove(); closeOpp(); }
    clearInterval(battleTimer);
    const tick = () => {
      const now = Date.now() + offset;
      if (b.status === "countdown") { $("lr-pktime").textContent = "Starts in " + Math.max(0, Math.ceil((Date.parse(b.starts_at) - now) / 1000)) + "s"; }
      else if (b.status === "active") {
        const left = Math.max(0, Math.ceil((Date.parse(b.ends_at) - now) / 1000));
        $("lr-pktime").textContent = `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
        if (left === 0) { clearInterval(battleTimer); setTimeout(() => poll(true), 600); }
      } else {
        $("lr-pktime").textContent = "Final";
        const r = $("lr-pkresult"); r.style.display = "block";
        r.textContent = b.outcome === "draw" ? "It's a draw!" : b.winner ? (b.winner === (a && a.username) ? "@" + a.username : "@" + c.username) + " wins!" : "Battle over";
        clearInterval(battleTimer);
      }
    };
    tick(); if (live) battleTimer = setInterval(tick, 500);
  }
  async function openOpp() {
    const key = S.battle.id; if (oppKey === key || !LK() || !S.provider_ready) return; oppKey = key;
    const t = await getToken("opponent"); if (!t.ok || oppKey !== key) return;
    try { const rm = new (LK().Room)({ adaptiveStream: true }); oppRoom = rm; wire(rm); await rm.connect(t.url, t.token);
      rm.remoteParticipants.forEach((pa) => pa.trackPublications.forEach((pub) => { if (pub.isSubscribed && pub.track) attachTrack(pub.track, pub, pa, rm); })); } catch (e) { oppKey = ""; }
  }
  function closeOpp() { if (oppRoom) { try { oppRoom.disconnect(); } catch (e) {} oppRoom = null; } oppKey = ""; }
  function renderInvites() {
    const box = $("lr-banner"); box.replaceChildren(); box.style.display = "none";
    const inv = (S.battle_invites || [])[0]; const mine = S.guests.mine;
    if (inv) {
      box.style.display = "block"; const c = el("div", "lv-note"); c.style.background = "#222"; c.style.color = "#fff";
      c.appendChild(el("span", "", `@${inv.from.username} invites you to a ${inv.duration_minutes}-minute PK battle.`));
      const y = el("button", "btn sm", "Accept"), n = el("button", "btn sm ghost", "Decline"); y.type = n.type = "button";
      y.addEventListener("click", async () => { const r = await api(`/live/api/battle/${inv.id}/respond`, { accept: true }); toast(r.ok ? "Battle accepted." : r.error); refetch(); });
      n.addEventListener("click", async () => { await api(`/live/api/battle/${inv.id}/respond`, { accept: false }); refetch(); });
      c.append(y, n); box.appendChild(c);
    } else if (mine && mine.status === "invited" && !IS_HOST) {
      box.style.display = "block"; const c = el("div", "lv-note"); c.style.background = "#222"; c.style.color = "#fff";
      c.appendChild(el("span", "", `${S.host.display_name} invited you on stage. If you accept, your ${mine.media === "audio" ? "microphone" : "camera and microphone"} will be shared with everyone watching.`));
      const y = el("button", "btn sm", "Accept & join"), n = el("button", "btn sm ghost", "No thanks"); y.type = n.type = "button";
      y.addEventListener("click", async () => { const r = await api(`/guests/${mine.invite_id}/respond`, { accept: true }); if (!r.ok) toast(r.error || "Couldn't join."); refetch(); });
      n.addEventListener("click", async () => { await api(`/guests/${mine.invite_id}/respond`, { accept: false }); refetch(); });
      c.append(y, n); box.appendChild(c);
    }
  }

  // ---------------------------------------------------------------- guests
  function renderGuests() {
    const g = $("lr-guests"); const have = new Set((S.guests.accepted || []).map((x) => x.identity));
    g.querySelectorAll(".lr-tile").forEach((t) => { if (t.dataset.key !== "me" && !have.has(t.dataset.key)) t.remove(); });
  }
  const askBtn = $("lr-askguest");
  if (askBtn) {
    askBtn.addEventListener("click", () => {
      const b = el("div"); b.appendChild(el("p", "", "The host decides. If they accept, you'll be asked once more before anyone sees or hears you."));
      const row = el("div", "lr-actions");
      [["video", "With camera"], ["audio", "Audio only"]].forEach(([m, l]) => { const x = el("button", "btn", l); x.type = "button"; x.addEventListener("click", async () => { closeSheet(); const r = await api("/guests/request", { media: m }); toast(r.ok ? "Request sent." : r.error); refetch(); }); row.appendChild(x); });
      b.appendChild(row); openSheet("Ask to join the stage", b);
    });
    $("lr-leaveguest").addEventListener("click", async () => { await api("/guests/leave", {}); refetch(); });
    $("lr-report").addEventListener("click", () => reportSheet(null, null));
  }

  // ---------------------------------------------------------------- user / report sheets
  function reportSheet(user, ev) {
    const b = el("div"); const sel = el("select", "lr-sel"); sel.setAttribute("aria-label", "Reason");
    (S.report_reasons || []).forEach((r) => { const o = el("option", "", r.replace(/_/g, " ")); o.value = r; sel.appendChild(o); });
    const det = el("input", "lr-sel"); det.type = "text"; det.maxLength = 300; det.placeholder = "Anything we should know? (optional)"; det.style.marginTop = "8px";
    const go = el("button", "btn block", "Send report"); go.type = "button"; go.style.marginTop = "10px";
    go.addEventListener("click", async () => { go.disabled = true; const r = await api("/report", { reason: sel.value, username: user && user.username, event_id: ev && ev.id, details: det.value }); closeSheet(); toast(r.ok ? (r.message || "Thanks. Our team will review it.") : r.error); });
    b.append(el("p", "muted", user ? `Report @${user.username}` : "Report this live"), sel, det, go); openSheet("Report", b);
  }
  function userSheet(u, ev) {
    const b = el("div"), head = el("div", "lr-row"); head.append(avatar(u)); const g = el("div", "grow"); g.append(el("b", "", u.display_name), el("small", "muted", "@" + u.username)); head.appendChild(g); b.appendChild(head);
    const act = (label, fn, cls) => { const x = el("button", "btn sm " + (cls || "ghost"), label); x.type = "button"; x.style.margin = "8px 8px 0 0"; x.addEventListener("click", async () => { closeSheet(); await fn(); }); b.appendChild(x); };
    const mod = (action, extra) => async () => { const r = await api("/mod", { action, username: u.username, ...(extra || {}) }); toast(r.ok ? "Done." : r.error || "Couldn't do that."); };
    const isMe = S.me.username === u.username, isHostU = (u.badges || []).includes("host");
    if (!isMe) act("Profile", () => { window.open("/u/" + encodeURIComponent(u.username), "_blank", "noopener"); });
    if (!isMe) act("Report", () => reportSheet(u, ev));
    if (S.me.can_moderate && !isMe && !isHostU) {
      if (ev) { act("Delete message", async () => { const r = await api("/delete", { event_id: ev.id }); toast(r.ok ? "Deleted." : r.error); }); act("Pin message", async () => { const r = await api("/pin", { event_id: ev.id }); toast(r.ok ? "Pinned." : r.error); }); }
      act("Mute", mod("mute")); act("Unmute", mod("unmute")); act("Timeout 5 min", mod("timeout", { minutes: 5 }));
      act("Remove from live", () => confirmAction(`Remove @${u.username}?`, mod("remove")), "danger");
    }
    if (IS_HOST && !isMe && !isHostU) {
      act("Block", () => confirmAction(`Block @${u.username} from your lives?`, mod("block")), "danger"); act("Unblock", mod("unblock"));
      act("Make moderator", mod("mod_add")); act("Remove moderator", mod("mod_remove"));
      if (S.session.guests_enabled) act("Invite on stage", async () => { const r = await api("/guests/invite", { username: u.username, media: "video" }); toast(r.ok ? "Invite sent." : r.error); refetch(); });
    }
    openSheet("@" + u.username, b);
  }
  function confirmAction(text, fn) { setTimeout(() => confirmBox("Are you sure?", text, "Yes", fn), 0); }
  $("lr-viewers").addEventListener("click", async () => {
    const r = await api("/state"); if (r.ok) { S = r.state; }
    const b = el("div"); const list = (S && S.viewers) || [];
    if (!list.length) b.appendChild(el("p", "muted", "No one to show yet."));
    list.forEach((v) => { const row = el("div", "lr-row"); row.appendChild(avatar(v)); const g = el("div", "grow"); g.append(el("b", "", v.display_name), el("small", "muted", "@" + v.username)); row.appendChild(g); const bt = el("button", "btn sm ghost", "View"); bt.type = "button"; bt.addEventListener("click", () => userSheet({ ...v, badges: v.badges || [] }, null)); row.appendChild(bt); b.appendChild(row); });
    openSheet(`Watching (${S ? S.viewer_count : 0})`, b);
  });
  $("lr-follow").addEventListener("click", async () => {
    const r = await api(`/u/${encodeURIComponent(root.dataset.host)}/follow`, {});
    if (!r.ok) { toast(r.error || "Couldn't update follow."); return; }
    S.host.follow_state = r.state; if (typeof r.followers === "number") S.host.followers = r.followers; applyState(false);
  });

  // ---------------------------------------------------------------- gifts
  const giftBtn = $("lr-giftbtn");
  if (giftBtn) giftBtn.addEventListener("click", () => {
    if (ended) return;
    const b = el("div"); const bal = el("p", "muted"); const upd = () => { bal.textContent = `Your balance: ${S.me.balance} coins`; }; upd();
    const grid = el("div", "lr-gifts"); const pending = {};
    S.gifts.forEach((g) => {
      const x = el("button", "lr-gift"); x.type = "button"; x.disabled = S.me.balance < g.price;
      x.append(el("span", "em", window.LiveGifts.emoji(g.code)), el("b", "", g.name), el("small", "", `${g.price} coins`));
      x.append(el("small", "", g.code === "mystery_box" ? "Surprise! Host gets 50" : g.diamonds ? `Shows ${g.diamonds} 💎 × ${g.diamond_value} (display only). Host gets ${g.host_coins}` : `Host gets ${g.host_coins}`));
      x.addEventListener("click", async () => {
        if (x.classList.contains("busy")) return;
        pending[g.code] = pending[g.code] || (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random());
        x.classList.add("busy"); x.disabled = true;
        const r = await api("/gift", { gift: g.code, request_id: pending[g.code] });
        x.classList.remove("busy");
        if (r.ok) {
          delete pending[g.code]; S.me.balance = r.balance; renderBalance(); upd();
          if (!r.duplicate && r.txn_id && !seenGifts.has(r.txn_id)) { seenGifts.add(r.txn_id); window.LiveGifts.play({ ...r.gift, gift_code: r.gift.code, diamonds: r.diamonds, diamond_value: r.diamond_value, sender_name: "You" }); }
          toast(r.message || "Gift sent!"); poll(false);
          grid.querySelectorAll(".lr-gift").forEach((bt, i) => { bt.disabled = S.me.balance < S.gifts[i].price; });
        } else {
          if (!r.network) delete pending[g.code];              // a definite answer: next tap is a new request. Network error: keep the id so a retry can't double charge.
          if (r.status === 402) { S.me.balance = r.balance != null ? r.balance : S.me.balance; upd(); }
          toast(r.error || "Couldn't send the gift."); x.disabled = S.me.balance < g.price && r.status !== 0;
        }
      });
      grid.appendChild(x);
    });
    b.append(bal, grid); b.appendChild(el("p", "lv-explain", "Coins stay inside CIRQ. Half of each gift goes to the creator; diamonds in an animation are for show only."));
    openSheet("Send a gift", b);
  });

  // ---------------------------------------------------------------- host controls
  const ctl = $("lr-ctl");
  if (ctl) {
    ctl.addEventListener("click", () => { loadState().then(buildControls); });
    $("lr-end").addEventListener("click", () => confirmBox("End your live?", "Everyone will be disconnected.", "End live", async () => { const r = await api("/end", {}); if (r.ok) { showEnded(); setTimeout(() => { location.href = root.dataset.dashboard; }, 1200); } else toast(r.error || "Couldn't end the live."); }));
  }
  function buildControls() {
    const b = el("div");
    const sw = (label, checked, fn) => { const l = el("label", "lv-switch"); const i = el("input"); i.type = "checkbox"; i.checked = checked; i.addEventListener("change", async () => { const r = await fn(i.checked); if (!r.ok) { i.checked = !i.checked; toast(r.error || "Couldn't change that."); } else refetch(); }); l.append(i, el("span", "", label)); return l; };
    b.appendChild(sw("Chat on", S.session.chat_enabled, (v) => api("/settings", { chat_enabled: v })));
    b.appendChild(sw("Allow guests on stage", S.session.guests_enabled, (v) => api("/settings", { guests_enabled: v })));
    const slow = el("select", "lr-sel"); slow.setAttribute("aria-label", "Slow mode");
    [[0, "Slow mode: off"], [3, "Slow mode: 3s"], [10, "Slow mode: 10s"], [30, "Slow mode: 30s"]].forEach(([v, l]) => { const o = el("option", "", l); o.value = v; if (+v === +S.session.slow_mode_seconds) o.selected = true; slow.appendChild(o); });
    slow.addEventListener("change", async () => { const r = await api("/settings", { slow_mode_seconds: +slow.value }); toast(r.ok ? "Updated." : r.error); refetch(); });
    b.appendChild(slow);
    // guests
    b.appendChild(el("h4", "", `On stage (${S.guests.accepted.length}/${S.guests.max})`));
    S.guests.accepted.forEach((g) => { const row = el("div", "lr-row"); const n = el("div", "grow"); n.append(el("b", "", g.display_name), el("small", "muted", "@" + g.username)); row.append(avatar(g), n);
      const m1 = el("button", "btn sm ghost", g.audio ? "Mute mic" : "Allow mic"); m1.type = "button"; m1.addEventListener("click", async () => { const r = await api(`/guests/${g.invite_id}/permissions`, { audio: !g.audio }); toast(r.ok ? "Updated." : r.error); loadState().then(buildControls); });
      const m2 = el("button", "btn sm ghost", g.video ? "Camera off" : "Camera on"); m2.type = "button"; if (g.media === "audio") m2.disabled = true; m2.addEventListener("click", async () => { const r = await api(`/guests/${g.invite_id}/permissions`, { video: !g.video }); toast(r.ok ? "Updated." : r.error); loadState().then(buildControls); });
      const rm = el("button", "btn sm danger", "Remove"); rm.type = "button"; rm.addEventListener("click", async () => { const r = await api(`/guests/${g.invite_id}/remove`, {}); toast(r.ok ? "Removed." : r.error); loadState().then(buildControls); });
      row.append(m1, m2, rm); b.appendChild(row); });
    if (S.guests.pending.length) b.appendChild(el("h4", "", "Requests and invites"));
    S.guests.pending.forEach((g) => { const row = el("div", "lr-row"); const n = el("div", "grow"); n.append(el("b", "", g.display_name), el("small", "muted", `@${g.username} · ${g.status === "requested" ? "asked to join" : "invited"} (${g.media})`)); row.append(avatar(g), n);
      if (g.status === "requested") { const y = el("button", "btn sm", "Accept"); y.type = "button"; y.addEventListener("click", async () => { const r = await api(`/guests/${g.invite_id}/respond`, { accept: true }); toast(r.ok ? "Accepted. They'll be asked to confirm." : r.error); loadState().then(buildControls); row.remove(); row.appendChild(y); }); const no = el("button", "btn sm ghost", "Decline"); no.type = "button"; no.addEventListener("click", async () => { await api(`/guests/${g.invite_id}/respond`, { accept: false }); loadState().then(buildControls); }); row.append(y, no); }
      else { const c = el("button", "btn sm ghost", "Cancel"); c.type = "button"; c.addEventListener("click", async () => { await api(`/guests/${g.invite_id}/remove`, {}); loadState().then(buildControls); }); row.appendChild(c); }
      b.appendChild(row); });
    // PK
    b.appendChild(el("h4", "", "PK battle"));
    const who = el("input", "lr-sel"); who.type = "text"; who.placeholder = "Opponent's username (they must be live)"; who.setAttribute("aria-label", "Opponent username");
    const mins = el("select", "lr-sel"); mins.style.marginTop = "8px"; mins.setAttribute("aria-label", "Duration"); (S.battle_minutes || [5]).forEach((m) => { const o = el("option", "", m + " minutes"); o.value = m; if (m === 5) o.selected = true; mins.appendChild(o); });
    const go = el("button", "btn block", "Invite to PK"); go.type = "button"; go.style.marginTop = "8px";
    go.addEventListener("click", async () => { go.disabled = true; const r = await api("/battle/invite", { username: who.value, minutes: +mins.value }); go.disabled = false; toast(r.ok ? "Invite sent. They have a short time to accept." : r.error); if (r.ok) closeSheet(); });
    const note = el("p", "lv-explain", "A PK battle is a timed gift contest between two lives. Only gifts sent while it's running count."); b.append(who, mins, go, note);
    if (S.battle && (S.battle.status === "countdown" || S.battle.status === "active")) { const e = el("button", "btn danger block", "End battle now"); e.type = "button"; e.style.marginTop = "8px"; e.addEventListener("click", async () => { const r = await api(`/live/api/battle/${S.battle.id}/end`, {}); toast(r.ok ? "Battle ended." : r.error); closeSheet(); refetch(); }); b.appendChild(e); }
    openSheet("Host controls", b);
  }

  // ---------------------------------------------------------------- end / leave
  function showEnded(reason, text) {
    if (ended && $("lr-ended").classList.contains("on")) return;
    ended = true; clearTimeout(pollTimer); clearInterval(battleTimer);
    if (text) $("lr-ended-txt").textContent = text; else if (reason === "ended_by_staff") $("lr-ended-txt").textContent = "This live was ended by CIRQ staff.";
    $("lr-ended").classList.add("on");
    try { if (room) room.disconnect(); if (oppRoom) oppRoom.disconnect(); } catch (e) {}
    $("lr-input").disabled = true;
  }
  $("lr-close").addEventListener("click", () => {
    if (IS_HOST) return $("lr-end").click();
    fetch(`${API}/leave`, { method: "POST", credentials: "same-origin", keepalive: true, headers: { "X-CSRFToken": csrf() } }).finally(() => { location.href = root.dataset.index; });
  });
  window.addEventListener("pagehide", () => { if (!IS_HOST && !ended) fetch(`${API}/leave`, { method: "POST", credentials: "same-origin", keepalive: true, headers: { "X-CSRFToken": csrf() } }); try { if (room) room.disconnect(); } catch (e) {} });

  // ---------------------------------------------------------------- boot
  window.LiveGifts.init($("lr-fx"));
  (async () => {
    if (!IS_HOST) {
      const j = await api("/join", {});
      if (!j.ok) { showEnded(null, j.error || "You can't watch this live."); return; }
      S = j.state; applyState(true);
    } else loadState(true);
  })();
})();
