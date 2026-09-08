/*
 * Entrance motion and number count-up.
 *
 * Two rules this file follows. First, nothing here is load-bearing: the
 * initial state lives in JavaScript, not CSS, so if this file fails to load
 * or the browser blocks it the page still renders fully. Second, it does
 * nothing at all when the OS asks for reduced motion.
 */
(function () {
  "use strict";

  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduced || typeof Motion === "undefined") return;

  const { animate, stagger, inView } = Motion;

  const EASE = [0.22, 0.61, 0.36, 1];

  // No count-up on the KPI figures, deliberately. Every number on this
  // dashboard is a measured value and the whole argument of the app is that
  // those numbers can be trusted, so a card that reads 104,163 on its way to
  // 104,431 is worse than one that just reads 104,431.

  // ------------------------------------------------------------------ entrance
  function enter(elements, options) {
    if (!elements.length) return;
    const controls = animate(
      elements,
      { opacity: [0, 1], transform: ["translateY(8px)", "translateY(0)"] },
      Object.assign({ duration: 0.42, easing: EASE }, options)
    );
    // An entrance that starts at opacity 0 means a stalled animation is a
    // blank page. Anything still running after a second is not decoration
    // any more, so jump it to the end.
    setTimeout(() => {
      try { controls.finish(); } catch (e) { /* already done */ }
    }, 1000);
  }

  function run() {
    enter(Array.from(document.querySelectorAll(".kpi")), { delay: stagger(0.05) });

    // Panels above the fold come in with the KPIs; the rest wait until they
    // are scrolled to, so a long page does not animate everything at once.
    const panels = Array.from(document.querySelectorAll("main > .panel, main > .grid > .panel"));
    const fold = window.innerHeight;
    const above = panels.filter((p) => p.getBoundingClientRect().top < fold);
    const below = panels.filter((p) => p.getBoundingClientRect().top >= fold);

    enter(above, { delay: stagger(0.06, { start: 0.08 }) });
    // Deliberately not hidden up front. A panel below the fold cannot be seen
    // before its observer fires anyway, and pre-hiding would leave it blank
    // for good if the observer never ran.
    below.forEach((panel) => {
      inView(panel, () => enter([panel], { duration: 0.36 }),
        { margin: "0px 0px -12% 0px" });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", run, { once: true });
  } else {
    run();
  }

  // Used by the assistant panel, which is shown and hidden rather than
  // rendered with the page.
  window.modelopsMotion = {
    popIn(el) {
      animate(
        el,
        { opacity: [0, 1], transform: ["translateY(10px) scale(.985)", "none"] },
        { duration: 0.24, easing: EASE }
      );
    },
  };
})();
