/* Two small pieces of motion, and the rule they both obey.

   The owner asked for text and number animation and sent three references. The
   question this file answers is not "should something move" but "what does the
   movement say", because the page already has three moving things - the hero
   typing itself, the shot chart filling one attempt at a time, the lineup
   figure changing - and a fourth kind of motion with nothing to say turns a
   page that demonstrates into a page that fidgets.

   So there are two, and each one means something.

   THE NUMBERS COUNT. 732 games, 53 held back, 18 tools. These are not round
   numbers chosen for a headline; they are the result of counting, and the
   count is the claim. A number that arrives by counting says what it is.

   ONE PHRASE IS MARKED. The sentence under the numbers draws the line the
   whole project rests on: the counting statistics are the league's, and the
   possessions, lineups and rates are ours. The marker is on our half of it,
   once, and nowhere else on the page. A highlighter used twice is a background
   colour.

   THE RULE, the same one the rest of the page follows:
     - once, when the thing is actually being looked at, then never again
     - on screen AND in the visible tab, because a background tab reports its
       sections as intersecting and would spend the animation on nobody
     - finished immediately for a visitor who asked for less motion */

(function () {
  "use strict";

  var COUNT_MS = 1100;   // the whole count, however large the number
  var MARK_MS = 620;     // the marker's sweep

  var reduced = window.matchMedia("(prefers-reduced-motion: reduce)");

  /* Run `fn` once, the first time `el` is both on screen and in a tab someone
     is looking at. Without the second condition the page plays itself out in a
     background tab and the visitor arrives after the end. */
  function whenSeen(el, fn) {
    if (!("IntersectionObserver" in window)) {
      fn();
      return;
    }
    var done = false;

    function settle() {
      if (done) return;
      if (!seen) return;
      if (document.visibilityState !== "visible") return;
      done = true;
      io.disconnect();
      document.removeEventListener("visibilitychange", settle);
      fn();
    }

    var seen = false;
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) seen = true;
      });
      settle();
    }, { threshold: 0.4 });

    io.observe(el);
    document.addEventListener("visibilitychange", settle);
  }

  /* Fast at the start and easing to a stop, so the last few numbers are
     readable rather than a blur that snaps. */
  function easeOut(t) {
    return 1 - Math.pow(1 - t, 3);
  }

  function countTo(el, target) {
    var started = null;

    function frame(now) {
      if (started === null) started = now;
      var t = Math.min(1, (now - started) / COUNT_MS);
      el.textContent = String(Math.round(easeOut(t) * target));
      if (t < 1) window.requestAnimationFrame(frame);
    }

    window.requestAnimationFrame(frame);
  }

  /* ---- the counted numbers ---- */

  var figures = Array.prototype.slice.call(document.querySelectorAll(".facts dt"));
  figures.forEach(function (el) {
    var text = el.textContent.trim();
    if (!/^\d+$/.test(text)) return;   // anything that is not a plain count is left alone
    var target = parseInt(text, 10);
    if (reduced.matches) return;       // it already reads the right number

    /* Nothing moves while the digits change. Each `dt` is a block filling its
       own grid column, so its box is the column's width whether it holds one
       digit or three, and the description under it never shifts. Pinning a
       width here would be guarding against a reflow the layout already
       prevents. */
    el.textContent = "0";

    whenSeen(el, function () {
      countTo(el, target);
    });
  });

  /* ---- the one marked phrase ---- */

  var marks = Array.prototype.slice.call(document.querySelectorAll(".marker"));
  marks.forEach(function (el) {
    if (reduced.matches) {
      el.classList.add("is-marked");
      return;
    }
    whenSeen(el, function () {
      window.setTimeout(function () {
        el.classList.add("is-marked");
      }, MARK_MS / 2);
    });
  });
})();
