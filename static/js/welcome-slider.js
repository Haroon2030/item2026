/**
 * سلايدر ترحيب غرفة القرار — تشغيل تلقائي + نقاط + أزرار (RTL).
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
    var timer = null;
    var reduced =
      window.matchMedia &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    var intervalMs = 5200;

    function show(i) {
      index = (i + slides.length) % slides.length;
      slides.forEach(function (slide, n) {
        var on = n === index;
        slide.classList.toggle("is-active", on);
        slide.setAttribute("aria-hidden", on ? "false" : "true");
        slide.removeAttribute("hidden");
      });
      dots.forEach(function (dot, n) {
        var on = n === index;
        dot.setAttribute("aria-selected", on ? "true" : "false");
        dot.classList.toggle("is-active", on);
      });
    }

    function go(delta) {
      show(index + delta);
      restart();
    }

    function restart() {
      if (timer) window.clearInterval(timer);
      timer = null;
      if (reduced || slides.length < 2) return;
      timer = window.setInterval(function () {
        show(index + 1);
      }, intervalMs);
    }

    if (prev) prev.addEventListener("click", function () { go(-1); });
    if (next) next.addEventListener("click", function () { go(1); });
    dots.forEach(function (dot) {
      dot.addEventListener("click", function () {
        var n = parseInt(dot.getAttribute("data-welcome-dot") || "0", 10);
        show(n);
        restart();
      });
    });

    root.addEventListener("mouseenter", function () {
      if (timer) window.clearInterval(timer);
      timer = null;
    });
    root.addEventListener("mouseleave", restart);
    root.addEventListener("focusin", function () {
      if (timer) window.clearInterval(timer);
      timer = null;
    });
    root.addEventListener("focusout", function (ev) {
      if (!root.contains(ev.relatedTarget)) restart();
    });

    show(0);
    restart();
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
