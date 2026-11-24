/* CIRQ as an installable app (PWA): service worker, install prompt, update prompt, offline awareness.
   Everything here is progressive: if a browser doesn't support something, the site simply works as a normal website. */
(() => {
  "use strict";
  const root = document.documentElement;
  const $ = (sel, scope = document) => scope.querySelector(sel);
  const $$ = (sel, scope = document) => [...scope.querySelectorAll(sel)];
  const script = document.currentScript;
  const SW_URL = (script && script.dataset.sw) || "/sw.js";
  const SNOOZE_KEY = "cirq.pwa.snoozeUntil", THEME_KEY = "cirq.theme", SNOOZE_DAYS = 14;

  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (_) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (_) { /* private mode */ } },
  };
  const standalone = () => window.matchMedia("(display-mode: standalone)").matches || window.matchMedia("(display-mode: fullscreen)").matches || window.navigator.standalone === true;
  const isIOS = () => /iphone|ipad|ipod/i.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  if (standalone()) root.classList.add("is-pwa");

  function notify(message, kind = "ok") {
    if (window.CIRQ && window.CIRQ.toast) { window.CIRQ.toast(message, kind); return; }
    const el = document.createElement("div");
    el.className = "pwa-toast"; el.setAttribute("role", "status"); el.textContent = message;
    document.body.append(el); setTimeout(() => el.remove(), 5000);
  }

  /* ---- theme: remember it so the offline page (which can't ask the server) matches the app ---- */
  const savedTheme = store.get(THEME_KEY);                                  // read first: the offline page must not overwrite it with its own default
  const theme = root.dataset.theme;
  if (!$("[data-offline-page]") && (theme === "dark" || theme === "light")) store.set(THEME_KEY, theme);

  /* ---- install: Android / desktop browsers hand us a prompt; iPhone and iPad need the Share-menu steps ---- */
  let deferred = null, bannerReady = false;
  const snoozed = () => Number(store.get(SNOOZE_KEY) || 0) > Date.now();
  const snooze = () => store.set(SNOOZE_KEY, String(Date.now() + SNOOZE_DAYS * 86400000));

  function renderInstallUi() {
    const installed = standalone(), ios = isIOS() && !installed;
    $$("[data-pwa-install]").forEach((b) => { b.hidden = !deferred || installed; });
    $$("[data-pwa-installed]").forEach((el) => { el.hidden = !installed; });
    $$("[data-pwa-ios]").forEach((el) => { el.hidden = !ios; });
    $$("[data-pwa-howto]").forEach((el) => { el.hidden = installed; });
    const banner = $("[data-pwa-banner]");
    if (!banner) return;
    const show = bannerReady && !installed && !snoozed() && (deferred || ios);
    banner.hidden = !show;
    const text = $("[data-pwa-banner-text]", banner);
    if (text && ios && !deferred) text.textContent = "Tap the Share button, then “Add to Home Screen”.";
  }
  window.addEventListener("beforeinstallprompt", (event) => { event.preventDefault(); deferred = event; renderInstallUi(); });
  window.addEventListener("appinstalled", () => { deferred = null; renderInstallUi(); notify("CIRQ is installed. Open it from your home screen."); });
  setTimeout(() => { bannerReady = true; renderInstallUi(); }, 3500);          // don't greet people with a banner the instant a page opens
  window.matchMedia("(display-mode: standalone)").addEventListener?.("change", renderInstallUi);

  async function install() {
    if (!deferred) return;
    const prompt = deferred; deferred = null; renderInstallUi();
    prompt.prompt();
    const choice = await prompt.userChoice.catch(() => null);
    if (choice && choice.outcome === "dismissed") snooze();
  }
  document.addEventListener("click", (event) => {
    if (event.target.closest("[data-pwa-install]")) { install(); return; }
    if (event.target.closest("[data-pwa-dismiss]")) { snooze(); renderInstallUi(); }
  });
  renderInstallUi();

  /* ---- service worker + updates ---- */
  if ("serviceWorker" in navigator && (location.protocol === "https:" || location.hostname === "localhost" || location.hostname === "127.0.0.1")) {
    const hadController = !!navigator.serviceWorker.controller;      // true if this isn't the very first visit
    let reloading = false;

    function offerUpdate(worker) {
      if ($(".pwa-update")) return;
      const bar = document.createElement("div");
      bar.className = "pwa-update"; bar.setAttribute("role", "alert");
      bar.innerHTML = '<span>A new version of CIRQ is ready.</span><button class="btn sm" type="button" data-update>Update</button><button class="icon-btn" type="button" data-later aria-label="Later">×</button>';
      $("[data-update]", bar).addEventListener("click", () => { $("[data-update]", bar).disabled = true; worker.postMessage({ type: "SKIP_WAITING" }); });
      $("[data-later]", bar).addEventListener("click", () => bar.remove());
      document.body.append(bar);
    }
    navigator.serviceWorker.addEventListener("controllerchange", () => {
      if (!hadController || reloading) return;                       // the first install takes control silently: no reload needed
      reloading = true; location.reload();                           // an update was applied: restart on the new version
    });
    window.addEventListener("load", () => {
      navigator.serviceWorker.register(SW_URL, { scope: "/" }).then((reg) => {
        if (reg.waiting && navigator.serviceWorker.controller) offerUpdate(reg.waiting);
        reg.addEventListener("updatefound", () => {
          const worker = reg.installing; if (!worker) return;
          worker.addEventListener("statechange", () => { if (worker.state === "installed" && navigator.serviceWorker.controller) offerUpdate(worker); });
        });
        const check = () => reg.update().catch(() => { /* offline: try again later */ });
        document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") check(); });     // coming back to the app
        setInterval(check, 60 * 60 * 1000);
      }).catch(() => { /* the site still works without it */ });
    });
  }

  /* ---- offline awareness ---- */
  let bar = null;
  function connection() {
    root.classList.toggle("is-offline", !navigator.onLine);
    if (!navigator.onLine && !bar && !$("[data-offline-page]")) {
      bar = document.createElement("div"); bar.className = "pwa-offline-bar"; bar.setAttribute("role", "status");
      bar.textContent = "You're offline. Some things won't load until you're back."; document.body.append(bar);
    } else if (navigator.onLine && bar) { bar.remove(); bar = null; notify("Back online."); }
  }
  window.addEventListener("online", connection); window.addEventListener("offline", connection); connection();

  /* ---- the offline page itself ---- */
  const page = $("[data-offline-page]");
  if (page) {
    const light = savedTheme === "light" || /(?:^|; )cirq_theme=light/.test(document.cookie);
    if (light) {
      root.dataset.theme = "light";
      const css = $("[data-pwa-theme-css]"); if (css && css.dataset.lightHref) css.href = css.dataset.lightHref;
      const meta = $('meta[name="theme-color"]'); if (meta) meta.content = "#f6f6f1";
    }
    const status = $("[data-offline-status]", page);
    const retry = async (manual) => {
      if (status) status.textContent = "Checking the connection…";
      try {
        const res = await fetch(page.dataset.health || "/health", { cache: "no-store" });
        if (res.ok) { location.reload(); return; }
      } catch (_) { /* still offline */ }
      if (status) status.textContent = manual ? "Still can't reach CIRQ. We'll keep trying." : "Waiting for a connection…";
    };
    $("[data-offline-retry]", page).addEventListener("click", () => retry(true));
    window.addEventListener("online", () => retry(false));
    setInterval(() => retry(false), 6000);
  }
})();
