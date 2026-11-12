/* Recovery code page: Copy / Download buttons. The server checks everything. */
(() => {
  "use strict";
  const box = document.querySelector("[data-code-box]");
  if (box) {
    const text = () => box.querySelector("[data-code-text]").textContent.trim();
    const copy = box.querySelector("[data-code-copy]"), dl = box.querySelector("[data-code-download]");
    copy && copy.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(text()); copy.textContent = "Copied"; }
      catch { const r = document.createRange(); r.selectNodeContents(box.querySelector("[data-code-text]")); const s = getSelection(); s.removeAllRanges(); s.addRange(r); copy.textContent = "Press copy"; }
      setTimeout(() => (copy.textContent = "Copy"), 1800);
    });
    dl && dl.addEventListener("click", () => {
      const who = dl.dataset.username ? `Account: ${dl.dataset.username}\n` : "";
      const body = `CIRQ recovery code\n${who}Code: ${text()}\n\nKeep this private. Anyone with your username and this code can reset your password.\nUse it at /recover if you forget your password. It works once, then CIRQ gives you a new one.\n`;
      const a = document.createElement("a");
      a.href = URL.createObjectURL(new Blob([body], { type: "text/plain" })); a.download = "cirq-recovery-code.txt";
      document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 2000);
    });
  }
})();
