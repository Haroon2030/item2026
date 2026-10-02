/**
 * سلايدر ترحيب غرفة القرار — النقطة النشطة شريط تقدّم CSS؛ انتهاؤه ينقل للشريحة التالية (RTL).
 */
(function () {
  "use strict";

  function init(root) {
    var slides = Array.prototype.slice.call(
      root.querySelectorAll("[data-welcome-slide]")
    );
    var dots = Array.prototype.slice.call(
      root.querySelectorAll("[data-welcome-dot]")
    );
    var prev = root.querySelector("[data-welcome-prev]");
    var next = root.querySelector("[data-welcome-next]");
    if (!slides.length) return;

    var index = 0;
    var reduced =
      window.matchMedia &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    var intervalMs = 5200;
    root.style.setProperty("--welcome-interval", intervalMs + "ms");

    var leaveTimer = null;

    function show(i) {
      var prevSlide = slides[index];
      index = (i + slides.length) % slides.length;
      if (prevSlide && prevSlide !== slides[index]) {
        slides.forEach(function (s) { s.classList.remove("is-leaving"); });
        prevSlide.classList.add("is-leaving");
        window.clearTimeout(leaveTimer);
        leaveTimer = window.setTimeout(function () {
          prevSlide.classList.remove("is-leaving");
        }, 900);
      }
      slides.forEach(function (slide, n) {
        var on = n === index;
        slide.classList.toggle("is-active", on);
        slide.setAttribute("aria-hidden", on ? "false" : "true");
        slide.removeAttribute("hidden");
      });
      dots.forEach(function (dot, n) {
        var on = n === index;
        dot.setAttribute("aria-selected", on ? "true" : "false");
        dot.classList.remove("is-active");
        if (on) {
          void dot.offsetWidth;
          dot.classList.add("is-active");
        }
      });
    }

    function go(delta) {
      show(index + delta);
    }

    function pause() { root.classList.add("is-paused"); }
    function resume() { root.classList.remove("is-paused"); }

    if (prev) prev.addEventListener("click", function () { go(-1); });
    if (next) next.addEventListener("click", function () { go(1); });
    dots.forEach(function (dot) {
      dot.addEventListener("click", function () {
        show(parseInt(dot.getAttribute("data-welcome-dot") || "0", 10));
      });
      dot.addEventListener("animationend", function (ev) {
        if (ev.animationName === "welcome-progress" && !reduced && slides.length > 1) {
          show(index + 1);
        }
      });
    });

    root.addEventListener("mouseenter", pause);
    root.addEventListener("mouseleave", resume);
    root.addEventListener("focusin", pause);
    root.addEventListener("focusout", function (ev) {
      if (!root.contains(ev.relatedTarget)) resume();
    });

    show(0);
  }

  function boot() {
    document.querySelectorAll("[data-welcome-slider]").forEach(init);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
