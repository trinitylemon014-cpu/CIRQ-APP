(() => {
  "use strict";
  const form = document.getElementById("lv-start");
  if (!form) return;
  const $ = (id) => document.getElementById(id);
  const csrf = () => (document.querySelector('meta[name="csrf-token"]') || {}).content || "";
  const title = $("lv-title"), err = $("lv-error"), btn = $("lv-submit"), msg = $("lv-check-msg"), vid = $("lv-preview");
  const val = (name) => (form.querySelector(`input[name=${name}]:checked`) || {}).value;
  let stream = null;
  title.addEventListener("input", () => { $("lv-count").textContent = `${title.value.length}/80`; });
  const stop = () => { if (stream) stream.getTracks().forEach((t) => t.stop()); stream = null; vid.srcObject = null; vid.style.display = "none"; };

  $("lv-check").addEventListener("click", async () => {
    stop(); msg.textContent = "Asking for permission…";
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) { msg.textContent = "This browser can't use the camera or microphone here. Try Chrome or Safari over https."; return; }
    const wantVideo = val("mode") !== "audio";
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: wantVideo });
      if (wantVideo) { vid.srcObject = stream; vid.style.display = "block"; await vid.play().catch(() => {}); }
      msg.textContent = wantVideo ? "Camera and microphone work. (The preview is only on your screen. It stops when you leave or start.)" : "Microphone works.";
    } catch (e) {
      const denied = e && (e.name === "NotAllowedError" || e.name === "SecurityError");
      msg.textContent = denied ? "Permission was blocked. Tap the lock or camera icon in your browser's address bar, allow Camera and Microphone for CIRQ, then check again."
        : e && e.name === "NotFoundError" ? "No camera or microphone was found on this device." : "Couldn't start the camera or microphone. Close other apps that use them and try again.";
    }
  });

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault(); err.style.display = "none";
    if (!title.value.trim()) { err.textContent = "Give your live a title."; err.style.display = "block"; title.focus(); return; }
    btn.disabled = true; stop();
    try {
      const r = await fetch(form.dataset.startUrl, { method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrf(), "X-Requested-With": "fetch" },
        body: JSON.stringify({ title: title.value, category: $("lv-cat").value, mode: val("mode"), visibility: val("visibility") }) });
      const d = await r.json().catch(() => ({}));
      if (r.ok && d.ok) { location.href = d.url; return; }
      err.textContent = d.error || "Couldn't start your live. Please try again."; err.style.display = "block";
    } catch (e) { err.textContent = "No connection. Check your internet and try again."; err.style.display = "block"; }
    btn.disabled = false;
  });
  window.addEventListener("pagehide", stop);
})();
