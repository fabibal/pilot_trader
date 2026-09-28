// "ÚJ" badges: mark every card / Consensus row / forecast row that is newer
// than this browser's previous look at the same view.
//
// dashboard.py renders the markers: each view's container carries
// data-view="<view>" and each item inside it data-ts="<ISO timestamp>". The
// newest timestamp seen per view is kept in localStorage. The baseline is read
// ONCE per page load, so a badge stays put for the whole visit (the views
// re-render as data changes) and is gone on the next visit. A first-ever visit
// only records the baseline (otherwise everything would be "new").
(function () {
  "use strict";
  var PREFIX = "pilot-trader:seen:";
  var baseline = {};   // view -> timestamp read at the first render this load
  var newest = {};     // view -> newest timestamp stored so far

  function read(view) {
    try { return window.localStorage.getItem(PREFIX + view); } catch (e) { return null; }
  }
  function write(view, ts) {
    try { window.localStorage.setItem(PREFIX + view, ts); } catch (e) { /* private mode */ }
  }

  function scan() {
    var items = document.querySelectorAll("[data-ts]");
    for (var i = 0; i < items.length; i++) {
      var el = items[i];
      var ts = el.getAttribute("data-ts");
      var holder = el.closest("[data-view]");
      if (!ts || !holder) continue;
      var view = holder.getAttribute("data-view");
      if (!(view in baseline)) {
        baseline[view] = read(view);
        newest[view] = baseline[view] || "";
      }
      if (baseline[view] && ts > baseline[view]) el.classList.add("is-new");
      if (ts > newest[view]) {
        newest[view] = ts;
        write(view, ts);
      }
    }
  }

  var queued = false;
  new MutationObserver(function () {
    if (queued) return;
    queued = true;
    window.requestAnimationFrame(function () { queued = false; scan(); });
  }).observe(document.documentElement, { childList: true, subtree: true });
})();
