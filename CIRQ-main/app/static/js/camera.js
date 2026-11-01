/* CIRQ live camera: take a photo or record a video inside the app, then hand it to the normal upload form.
 *
 * Any button with  data-camera="<css selector of a file input>"  opens it. Options (all optional):
 *   data-camera-modes="photo,video"   which modes to offer (default both)
 *   data-camera-max="60"              longest recording in seconds
 *   data-camera-max-mb="60"           largest recording in MB (recording stops before it is exceeded)
 * The captured file is placed in that input and a "change" event is fired, so every existing form, preview and
 * server-side validation keeps working exactly as if the person had picked the file from their gallery.
 * Browsers without getUserMedia / MediaRecorder fall back to the phone's own camera app (<input capture>).
 */
(() => {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const SVG = (n, cls = "") => `<svg class="i ${cls}" aria-hidden="true"><use href="#i-${n}"/></svg>`;
  const HAS_STREAM = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
  const HAS_RECORDER = typeof window.MediaRecorder !== "undefined";
  const fmt = (t) => `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, "0")}`;

  function toast(msg, kind = "error") {
    let box = $(".flashes");
    if (!box) { box = document.createElement("div"); box.className = "flashes"; document.body.append(box); }
    const el = document.createElement("div"); el.className = "flash " + kind;
    const span = document.createElement("span"); span.textContent = msg; el.append(span); box.append(el);
    setTimeout(() => el.remove(), 6000);
  }

  // Safari records MP4 (H.264); Chrome/Firefox record WebM. The server accepts both.
  function pickMime() {
    if (!HAS_RECORDER) return "";
    const list = ["video/mp4;codecs=avc1.42E01E,mp4a.40.2", "video/mp4", "video/webm;codecs=vp9,opus", "video/webm;codecs=vp8,opus", "video/webm"];
    return list.find((t) => { try { return MediaRecorder.isTypeSupported(t); } catch (_) { return false; } }) || "";
  }

  function setFile(input, file) {
    const dt = new DataTransfer(); dt.items.add(file); input.files = dt.files;
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }

  /* ---- fallback: the device's own camera app ---- */
  function nativeCapture(input, modes) {
    const tmp = document.createElement("input"); tmp.type = "file"; tmp.hidden = true;
    tmp.accept = modes.includes("photo") && modes.includes("video") ? "image/*,video/*" : modes.includes("video") ? "video/*" : "image/*";
    tmp.setAttribute("capture", "environment");
    tmp.addEventListener("change", () => { if (tmp.files[0]) setFile(input, tmp.files[0]); tmp.remove(); });
    document.body.append(tmp); tmp.click();
  }

  function open(input, opts) {
    // No MediaRecorder (very old browsers): offer photos only, or hand video over to the phone's own camera app.
    const modes = HAS_RECORDER ? opts.modes : opts.modes.filter((m) => m !== "video");
    if (!HAS_STREAM || !modes.length) { nativeCapture(input, opts.modes); return; }

    let stream = null, facing = "user", mode = modes.includes("photo") ? "photo" : "video";
    let recorder = null, chunks = [], recStart = 0, timer = null, result = null, bytes = 0;

    // A <dialog> opened with showModal() sits in the top layer, so the camera also appears above the "New story" sheet.
    const dlg = document.createElement("dialog"); dlg.className = "cam"; dlg.setAttribute("aria-label", "Camera");
    dlg.innerHTML = `
      <video class="cam-view" autoplay playsinline muted></video>
      <div class="cam-review" hidden></div>
      <div class="cam-top">
        <button type="button" class="cam-ic" data-c-close aria-label="Close camera">${SVG("x")}</button>
        <span class="cam-timer" data-c-timer hidden>0:00</span>
        <button type="button" class="cam-ic" data-c-flip aria-label="Switch camera">${SVG("flip")}</button>
      </div>
      <p class="cam-msg" data-c-msg role="status" hidden></p>
      <div class="cam-bottom">
        <div class="cam-modes" data-c-modes>
          <button type="button" data-c-mode="photo">Photo</button><button type="button" data-c-mode="video">Video</button>
        </div>
        <div class="cam-live">
          <span class="cam-spacer"></span>
          <button type="button" class="cam-shutter" data-c-shutter aria-label="Take photo"><i></i></button>
          <span class="cam-spacer"></span>
        </div>
        <div class="cam-confirm" data-c-confirm hidden>
          <button type="button" class="btn ghost" data-c-retake>${SVG("retake")} Retake</button>
          <button type="button" class="btn" data-c-use>Use this</button>
        </div>
      </div>`;
    document.body.append(dlg);
    const q = (s) => $(s, dlg);
    const view = q(".cam-view"), review = q(".cam-review"), msg = q("[data-c-msg]"), timerEl = q("[data-c-timer]");
    const shutter = q("[data-c-shutter]"), live = q(".cam-live"), confirmBar = q("[data-c-confirm]"), modesBar = q("[data-c-modes]");

    const say = (t) => { msg.textContent = t || ""; msg.hidden = !t; };
    const stopStream = () => { if (stream) { stream.getTracks().forEach((t) => t.stop()); stream = null; } view.srcObject = null; };
    function cleanup() {
      clearInterval(timer); if (recorder && recorder.state !== "inactive") { try { recorder.ondataavailable = null; recorder.onstop = null; recorder.stop(); } catch (_) {} }
      stopStream(); if (result && result.url) URL.revokeObjectURL(result.url); dlg.remove();
    }

    async function start() {
      stopStream(); say("");
      const base = { video: { facingMode: facing, width: { ideal: 1280 }, height: { ideal: 1280 } } };
      try {
        stream = await navigator.mediaDevices.getUserMedia({ ...base, audio: mode === "video" });
      } catch (err) {
        if (mode === "video" && err && (err.name === "NotAllowedError" || err.name === "NotFoundError" || err.name === "OverconstrainedError")) {
          try { stream = await navigator.mediaDevices.getUserMedia(base); say("Microphone unavailable: this video will have no sound."); }
          catch (e2) { return fatal(e2); }
        } else return fatal(err);
      }
      view.srcObject = stream; view.classList.toggle("mirror", facing === "user");
      view.play().catch(() => {});
    }
    function fatal(err) {
      const n = err && err.name;
      const text = n === "NotAllowedError" || n === "SecurityError" ? "Camera access is blocked. Allow the camera for this site in your browser settings, then try again."
        : n === "NotFoundError" || n === "OverconstrainedError" ? "No camera was found on this device." : "The camera couldn't be started.";
      dlg.close(); toast(text);
    }

    function paintMode() {
      dlg.querySelectorAll("[data-c-mode]").forEach((b) => { b.hidden = !modes.includes(b.dataset.cMode); b.classList.toggle("on", b.dataset.cMode === mode); });
      modesBar.hidden = modes.length < 2;
      shutter.classList.toggle("video", mode === "video"); shutter.setAttribute("aria-label", mode === "video" ? "Start recording" : "Take photo");
    }

    /* ---- photo ---- */
    function takePhoto() {
      if (!view.videoWidth) return;
      const max = 1920, k = Math.min(1, max / Math.max(view.videoWidth, view.videoHeight));
      const c = document.createElement("canvas"); c.width = Math.round(view.videoWidth * k); c.height = Math.round(view.videoHeight * k);
      c.getContext("2d").drawImage(view, 0, 0, c.width, c.height);
      c.toBlob((blob) => {
        if (!blob) { toast("Couldn't take the photo. Please try again."); return; }
        showReview(new File([blob], `photo-${Date.now()}.jpg`, { type: "image/jpeg" }), "image");
      }, "image/jpeg", 0.92);
    }

    /* ---- video ---- */
    function startRecording() {
      if (!stream) return; chunks = []; bytes = 0;
      const mimeType = pickMime();
      try { recorder = new MediaRecorder(stream, mimeType ? { mimeType, videoBitsPerSecond: 2500000 } : undefined); }
      catch (_) { toast("Video recording isn't supported in this browser. Use the gallery option instead."); return; }
      recorder.ondataavailable = (e) => {
        if (!e.data || !e.data.size) return; chunks.push(e.data); bytes += e.data.size;
        if (opts.maxBytes && bytes > opts.maxBytes * 0.95) stopRecording();           // stop before the server's size limit
      };
      recorder.onstop = () => {
        clearInterval(timer); timerEl.hidden = true; shutter.classList.remove("rec");
        const type = (recorder.mimeType || mimeType || "video/webm").split(";")[0];
        const blob = new Blob(chunks, { type });
        if (!blob.size) { toast("Nothing was recorded. Please try again."); live.hidden = false; return; }
        const ext = type.includes("mp4") ? "mp4" : "webm";
        showReview(new File([blob], `video-${Date.now()}.${ext}`, { type }), "video");
      };
      recorder.start(1000); recStart = Date.now();
      shutter.classList.add("rec"); shutter.setAttribute("aria-label", "Stop recording"); modesBar.hidden = true; timerEl.hidden = false; timerEl.textContent = `0:00 / ${fmt(opts.maxSec)}`;
      timer = setInterval(() => {
        const s = (Date.now() - recStart) / 1000; timerEl.textContent = `${fmt(s)} / ${fmt(opts.maxSec)}`;
        if (s >= opts.maxSec) stopRecording();
      }, 250);
    }
    function stopRecording() { if (recorder && recorder.state === "recording") recorder.stop(); }

    /* ---- review: retake or use ---- */
    function showReview(file, kind) {
      result = { file, kind, url: URL.createObjectURL(file) };
      stopStream();
      review.textContent = "";
      const el = document.createElement(kind === "video" ? "video" : "img"); el.src = result.url;
      if (kind === "video") { el.controls = true; el.autoplay = true; el.loop = true; el.playsInline = true; } else el.alt = "Photo preview";
      review.append(el); review.hidden = false; view.hidden = true;
      live.hidden = true; modesBar.hidden = true; confirmBar.hidden = false; q("[data-c-flip]").hidden = true;
      q("[data-c-use]").textContent = kind === "video" ? "Use video" : "Use photo";
    }
    function retake() {
      if (result) URL.revokeObjectURL(result.url); result = null;
      review.hidden = true; review.textContent = ""; view.hidden = false; live.hidden = false; confirmBar.hidden = true; q("[data-c-flip]").hidden = false;
      paintMode(); start();
    }

    dlg.addEventListener("click", (e) => {
      const b = e.target.closest("button"); if (!b) return;
      if (b.hasAttribute("data-c-close")) { dlg.close(); return; }
      if (b.hasAttribute("data-c-flip")) { facing = facing === "user" ? "environment" : "user"; start(); return; }
      if (b.dataset.cMode) { if (recorder && recorder.state === "recording") return; mode = b.dataset.cMode; paintMode(); start(); return; }
      if (b.hasAttribute("data-c-shutter")) {
        if (mode === "photo") takePhoto();
        else if (recorder && recorder.state === "recording") stopRecording(); else startRecording();
        return;
      }
      if (b.hasAttribute("data-c-retake")) { retake(); return; }
      if (b.hasAttribute("data-c-use")) { const f = result && result.file; dlg.close(); if (f) setFile(input, f); }
    });
    dlg.addEventListener("close", cleanup);
    dlg.addEventListener("cancel", () => { /* Esc: handled by the close event */ });
    document.addEventListener("visibilitychange", function onHide() {
      if (!dlg.isConnected) { document.removeEventListener("visibilitychange", onHide); return; }
      if (document.hidden) stopRecording();
    });

    paintMode(); dlg.showModal(); start();
  }

  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-camera]"); if (!btn) return;
    e.preventDefault();
    const input = document.querySelector(btn.dataset.camera);
    if (!input) return;
    const modes = (btn.dataset.cameraModes || "photo,video").split(",").map((x) => x.trim()).filter(Boolean);
    open(input, {
      modes: modes.length ? modes : ["photo", "video"],
      maxSec: +btn.dataset.cameraMax || 60,
      maxBytes: (+btn.dataset.cameraMaxMb || 0) * 1048576,
    });
  });
})();
