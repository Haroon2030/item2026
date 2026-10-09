(function () {
  "use strict";

  // وضع المجموعة في تحليل المبيعات: الصفحة تصل بهيكل فارغ، وهذا السكربت يجلب أرقام المجموعة
  // (بطاقات + جدولا الفروع + المرتجعات) من الخادم ويستبدل بها الأجزاء الثلاثة بلا إعادة تحميل.
  var TIMEOUT_MS = 15 * 60 * 1000;

  function readUrl() {
    var seed = document.getElementById("sales-gm-url");
    if (!seed) return "";
    try {
      return String(JSON.parse(seed.textContent || '""') || "");
    } catch (e) {
      return "";
    }
  }

  function slot(name) {
    return document.querySelector('[data-gm-slot="' + name + '"]');
  }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  var SKEL = new Array(7).join('<span class="gm-skel-card"><i></i><b></b><u></u></span>');

  // أثناء الحساب: هياكل بطاقات متحركة + شريط تقدّم بدل مربع نص فارغ، وفي الخطأ رسالة وزر إعادة
  function setLoadingText(text, isError, url, sec) {
    Array.prototype.forEach.call(document.querySelectorAll("[data-gm-loading]"), function (p, idx) {
      p.className = "gm-loading" + (isError ? " is-error" : "");
      if (isError) {
        p.innerHTML = esc(text) + ' <button type="button" class="gm-retry">إعادة المحاولة</button>';
        var btn = p.querySelector(".gm-retry");
        if (btn) btn.addEventListener("click", function () { load(url); });
        return;
      }
      var pct = Math.min(94, Math.round(100 * (1 - Math.exp(-(sec || 0) / 40))));
      p.innerHTML =
        (idx === 0 ? '<span class="gm-skel" aria-hidden="true">' + SKEL + "</span>" : "") +
        '<span class="gm-msg">' + esc(text) + "</span>" +
        '<span class="gm-bar" aria-hidden="true"><i style="width:' + pct + '%"></i></span>' +
        '<span class="gm-hint">أول مرة تُحسب أرقام كل المجموعات معًا ثم تُحفظ، فاختيار أي مجموعة بعدها يظهر فورًا.</span>';
    });
  }

  function apply(data) {
    var map = { board: data.board_html, tables: data.tables_html, returns: data.returns_html };
    Object.keys(map).forEach(function (name) {
      var el = slot(name);
      if (el) el.innerHTML = map[name] || "";
    });
    var strong = document.querySelector("#sales-gm-scope strong");
    if (strong && data.group_name) strong.textContent = data.group_name;
    // fit-kpi.js يعيد ملاءمة أرقام البطاقات عند resize، وسكربت المرتجعات يرسم قائمته من JSON الجديد
    try { window.dispatchEvent(new Event("resize")); } catch (e) { /* ignore */ }
    if (typeof window.salesReturnsInit === "function") window.salesReturnsInit();
  }

  function load(url) {
    var started = Date.now();
    var base = "جاري حساب أرقام المجموعة";
    setLoadingText(base + "…", false, url, 0);
    var tick = setInterval(function () {
      var sec = Math.round((Date.now() - started) / 1000);
      setLoadingText(base + " — " + sec + " ث", false, url, sec);
    }, 1000);

    var ctrl = typeof AbortController !== "undefined" ? new AbortController() : null;
    var abortTimer = ctrl ? setTimeout(function () { try { ctrl.abort(); } catch (e) { /* ignore */ } }, TIMEOUT_MS) : null;

    fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" }, cache: "no-store", signal: ctrl ? ctrl.signal : undefined })
      .then(function (r) {
        return r.text().then(function (text) {
          var data = null;
          try { data = text ? JSON.parse(text) : null; } catch (e) { data = null; }
          if (!r.ok || !data || data.ok === false) {
            throw new Error((data && data.error) || (r.ok ? "استجابة غير صالحة" : "HTTP " + r.status));
          }
          return data;
        });
      })
      .then(function (data) {
        clearInterval(tick);
        if (abortTimer) clearTimeout(abortTimer);
        apply(data);
      })
      .catch(function (err) {
        clearInterval(tick);
        if (abortTimer) clearTimeout(abortTimer);
        var msg = String((err && err.message) || "تعذّر حساب أرقام المجموعة");
        if (err && err.name === "AbortError") msg = "انتهت المهلة — الاستعلام بطيء جداً لهذه الفترة، جرّب فترة أقصر";
        setLoadingText(msg, true, url);
      });
  }

  function init() {
    var url = readUrl();
    if (url) load(url);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
