/**
 * بطاقات مؤشرات تحليل المبيعات تفاعلية: الضغط على بطاقة (أو Enter / Space) ينقلك
 * إلى الجدول أو المخطط الذي تُلخّصه، ويومض القسم الهدف لحظة ليُعرف مكانه.
 */
(function () {
  "use strict";

  // فئة البطاقة -> جزء من عنوان القسم الهدف
  var TARGETS = [
    ["dash-kpi-glass-total", "مبيعات نقاط البيع"],
    ["dash-kpi-glass-pos", "مبيعات نقاط البيع"],
    ["dash-kpi-glass-wholesale", "مبيعات نظام المبيعات"],
    ["dash-kpi-glass-returns", "الفروع الأكثر مرتجع"],
    ["dash-kpi-glass-visit", "أكثر المستخدمين بيعا"],
    ["dash-kpi-glass-branches", "مبيعات نقاط البيع"],
  ];

  function norm(s) {
    return String(s || "")
      .replace(/[ًٌٍَُِّْ]/g, "")
      .replace(/ا[ًٌٍ]/g, "ا")
      .replace(/[أإآ]/g, "ا")
      .replace(/ة/g, "ه")
      .replace(/\s+/g, " ")
      .trim();
  }

  function findSection(fragment) {
    var want = norm(fragment);
    var heads = document.querySelectorAll("main section h3");
    for (var i = 0; i < heads.length; i++) {
      if (norm(heads[i].textContent).indexOf(want) >= 0) {
        return heads[i].closest("section");
      }
    }
    return null;
  }

  function targetFor(card) {
    for (var i = 0; i < TARGETS.length; i++) {
      if (card.classList.contains(TARGETS[i][0])) return findSection(TARGETS[i][1]);
    }
    return null;
  }

  function jump(card) {
    var section = targetFor(card);
    if (!section) return;
    var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    section.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "start" });
    section.classList.remove("kpi-target-pulse");
    void section.offsetWidth;
    section.classList.add("kpi-target-pulse");
    setTimeout(function () {
      section.classList.remove("kpi-target-pulse");
    }, 1100);
  }

  function init() {
    var cards = document.querySelectorAll(".sales-ov-kpis .dash-kpi-card");
    cards.forEach(function (card) {
      if (!targetFor(card)) return;
      card.classList.add("is-jumpable");
      card.setAttribute("tabindex", "0");
      card.setAttribute("role", "button");
      var label = card.querySelector(".dash-kpi-label");
      card.setAttribute("aria-label", "الانتقال إلى: " + (label ? label.textContent.trim() : ""));
      card.addEventListener("click", function () {
        jump(card);
      });
      card.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter" || ev.key === " ") {
          ev.preventDefault();
          jump(card);
        }
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
