(function () {
  "use strict";
  // مؤشر تحميل خفيف: شريط رفيع أعلى الصفحة + شارة «جاري التحميل…» تظهر إن تأخر الانتقال أكثر من لحظة
  var DELAY = 250;
  var timer = null;
  var el = null;

  function build() {
    if (el) return el;
    el = document.createElement("div");
    el.className = "nav-loading";
    el.setAttribute("role", "status");
    el.setAttribute("aria-live", "polite");
    el.innerHTML = '<span class="nav-loading-bar"></span>' +
      '<span class="nav-loading-chip">' +
      '<button type="button" class="nav-loading-cancel" aria-label="إلغاء التحميل" title="إلغاء"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg></button>' +
      '<i class="nav-loading-spin"></i>' +
      '<span class="nav-loading-text">جاري التحميل<b class="nav-loading-dots" aria-hidden="true"><i></i><i></i><i></i></b></span>' +
      '<span class="nav-loading-track" aria-hidden="true"></span></span>';
    el.querySelector(".nav-loading-cancel").addEventListener("click", cancel);
    document.body.appendChild(el);
    return el;
  }

  function show() {
    clearTimeout(timer);
    timer = setTimeout(function () { build().classList.add("is-on"); }, DELAY);
  }

  // إلغاء الانتقال الجاري والبقاء في الصفحة الحالية
  function cancel() {
    try { window.stop(); } catch (err) { /* لا شيء */ }
    hide();
  }

  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && el && el.classList.contains("is-on")) cancel();
  });

  function hide() {
    clearTimeout(timer);
    if (el) el.classList.remove("is-on");
  }

  document.addEventListener("click", function (e) {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var a = e.target.closest && e.target.closest("a[href]");
    if (!a || a.target === "_blank" || a.hasAttribute("download")) return;
    var href = a.getAttribute("href") || "";
    if (!href || href.charAt(0) === "#" || /^(javascript|mailto|tel):/i.test(href)) return;
    var url;
    try { url = new URL(a.href, location.href); } catch (err) { return; }
    if (url.origin !== location.origin) return;
    if (url.pathname === location.pathname && url.search === location.search) return;
    show();
  });

  document.addEventListener("submit", function (e) {
    var f = e.target;
    if (e.defaultPrevented || !f || (f.target && f.target !== "_self")) return;
    show();
  });

  window.addEventListener("pageshow", hide); // الرجوع من الكاش
  window.addEventListener("pagehide", function () { clearTimeout(timer); });
})();
