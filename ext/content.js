"use strict";
// Mirror background state into the DOM so the page — and ext/tryout.py — can
// read it without a devtools session. Log lines are drained from the
// background here and buffered in the DOM; tryout.py reads and clears them.
setInterval(async () => {
  try {
    const s = await browser.runtime.sendMessage("stats");
    if (s) document.documentElement.dataset.unslop = JSON.stringify(s);

    const lines = await browser.runtime.sendMessage("drain");
    if (lines && lines.length) {
      const el = document.documentElement;
      const cur = JSON.parse(el.dataset.unslopLog || "[]");
      cur.push(...lines);
      while (cur.length > 400) cur.shift();
      el.dataset.unslopLog = JSON.stringify(cur);
    }
  } catch (e) { /* background not up yet */ }
}, 1000);
