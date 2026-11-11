/* Gift / loan form helpers: quick-amount chips and the live "what you are agreeing to" numbers. Purely cosmetic: the server checks everything. */
(() => {
  "use strict";
  const form = document.querySelector("[data-lend-form]");
  if (!form) return;
  const $ = (s) => form.querySelector(s);
  const amount = $("[data-amount]");
  form.querySelectorAll("[data-fill]").forEach((b) => b.addEventListener("click", () => { amount.value = b.dataset.fill; update(); amount.focus(); }));
  const due = $("[data-due]"), fee = $("[data-fee]");
  function update() {
    const a = parseInt(amount.value, 10);
    const set = (sel, text) => { const el = $(sel); if (el) el.textContent = text; };
    set("[data-amount-text]", a > 0 ? `${a} coins` : "the coins");
    if (due && due.value) {
      const d = new Date(due.value + "T12:00:00");
      set("[data-due-text]", d.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short", year: "numeric" }));
    }
    if (fee) {
      const p = Math.max(0, Math.min(parseInt(fee.value, 10) || 0, parseInt(fee.max, 10) || 100));
      set("[data-fee-text]", p ? `${p}%` : "0% (no)");
      set("[data-fee-example]", a > 0 && p > 0 ? ` (for example, if 40 of the ${a} coins are unpaid the fee is ${Math.max(1, Math.ceil(40 * p / 100))} coins)` : "");
    }
  }
  [amount, due, fee].forEach((el) => el && el.addEventListener("input", update));
  update();
})();
