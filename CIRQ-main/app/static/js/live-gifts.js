/* Gift animations. Rendered in a pointer-events:none overlay with CSS transform/opacity only, so they can't block taps or the video.
   Max 3 at once; extras wait in a short queue (low-value gifts are dropped when it is long). */
(() => {
  "use strict";
  const EMOJI = { red_rose: "🌹", love_heart: "💖", mystery_box: "🎁", golden_crown: "👑", treasure_chest: "🧰", sports_car: "🏎️", diamond_ring: "💍", private_jet: "🛩️" };
  const BOX_OUT = "🎉";
  let layer = null, running = 0, queue = [];
  const MAX = 3;
  const el = (t, c, x) => { const e = document.createElement(t); if (c) e.className = c; if (x != null) e.textContent = x; return e; };

  function build(g) {
    const tier = g.tier || "small", code = g.gift_code || g.code || "";
    const f = el("div", "fx " + tier + (code === "sports_car" ? " car" : "") + (code === "private_jet" ? " jet" : "") + (code === "mystery_box" ? " box" : "") + (tier === "premium" ? " sparkle" : ""));
    f.style.marginLeft = (Math.random() * 40 - 20) + "px";
    const big = el("div", "big", EMOJI[code] || "🎁"); f.appendChild(big);
    if (code === "mystery_box") {
      const o = el("div", "big open", BOX_OUT); o.style.position = "absolute"; o.style.top = "-6px"; f.appendChild(o);
      window.setTimeout(() => { big.textContent = "✨"; }, 1100);
    }
    if (g.diamonds) {
      const d = el("div", "dia"); d.setAttribute("aria-hidden", "true");
      for (let i = 0; i < g.diamonds; i++) { const x = el("i", "", "💎"); x.style.animationDelay = (0.3 + i * 0.08) + "s"; d.appendChild(x); }
      f.appendChild(d);
    }
    const who = g.sender_name || g.sender || "Someone";
    f.appendChild(el("div", "cap", `${who} sent ${g.name || "a gift"}`));
    return f;
  }
  function run(g) {
    running++;
    const f = build(g); layer.appendChild(f);
    const ms = { small: 2300, medium: 3100, large: 3900, premium: 5100 }[g.tier || "small"] || 2500;
    window.setTimeout(() => { f.remove(); running--; next(); }, ms + 150);
  }
  function next() { while (running < MAX && queue.length) run(queue.shift()); }
  window.LiveGifts = {
    init(l) { layer = l; },
    play(g) {
      if (!layer) return;
      if (queue.length > 8) { const i = queue.findIndex((q) => (q.tier || "small") === "small"); if (i >= 0) queue.splice(i, 1); else if ((g.tier || "small") === "small") return; }
      queue.push(g); next();
    },
    emoji: (code) => EMOJI[code] || "🎁",
  };
})();
