/* Pink Cloud - shared Back button (library.html, export.html).
   Markup: <a class="btn btn--ghost btn--sm pc-back" data-back href="./index.html"
             data-back-fallback="#someNavLink">...</a>
   Click goes back one step in history when the previous page is ours
   (same origin), so filters like library ?q= come back as they were.
   Opened from a bookmark, a new tab or another site, it navigates to the
   fallback instead: the href of the element named by data-back-fallback
   when that element has one, else the button's own href. */
(function () {
  "use strict";

  function cameFromHere() {
    if (!document.referrer || history.length < 2) return false;
    try { return new URL(document.referrer).origin === location.origin; } catch (e) { return false; }
  }

  function fallbackHref(btn) {
    var sel = btn.getAttribute("data-back-fallback");
    var target = sel ? document.querySelector(sel) : null;
    var href = target && target.getAttribute("href");
    return href || btn.getAttribute("href") || "./index.html";
  }

  function wire(btn) {
    btn.addEventListener("click", function (e) {
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.button === 1) {
        btn.href = fallbackHref(btn); /* new-tab clicks get the real target */
        return;
      }
      e.preventDefault();
      if (cameFromHere()) history.back();
      else location.href = fallbackHref(btn);
    });
    /* keep the hover/status-bar URL honest once page scripts set links */
    btn.addEventListener("mouseenter", function () { btn.href = fallbackHref(btn); });
    btn.addEventListener("focus", function () { btn.href = fallbackHref(btn); });
  }

  function init() {
    var list = document.querySelectorAll("[data-back]");
    for (var i = 0; i < list.length; i++) wire(list[i]);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
