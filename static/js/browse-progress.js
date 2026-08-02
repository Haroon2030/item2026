/* شريط تقدم صفحة تصفح المجموعات — ملف خارجي لأن CSP يمنع السكربتات الداخلية */
(function () {
  function init() {
    var form = document.querySelector('.browse-form');
    var overlay = document.getElementById('browse-loading');
    var label = document.getElementById('browse-loading-label');
    var cancelBtn = document.getElementById('browse-loading-cancel');
    if (!form || !overlay) return;

    var timer = null;
    var submitTimer = null;
    var pct = 1;

    function hideBar() {
      if (timer) { clearInterval(timer); timer = null; }
      if (submitTimer) { clearTimeout(submitTimer); submitTimer = null; }
      overlay.hidden = true;
      overlay.classList.remove('is-active');
      var btn = document.getElementById('browse-submit');
      if (btn) btn.disabled = false;
    }

    function showBar() {
      pct = 1;
      if (label) label.textContent = '1%';
      overlay.hidden = false;
      overlay.classList.add('is-active');
      var btn = document.getElementById('browse-submit');
      if (btn) btn.disabled = true;
      timer = setInterval(function () {
        if (pct < 30) pct += 2;
        else if (pct < 60) pct += 1;
        else if (pct < 85) pct += 0.5;
        else pct += 0.15;
        pct = Math.min(pct, 97);
        if (label) label.textContent = Math.round(pct) + '%';
      }, 150);
    }

    // الرجوع بزر Back يستعيد الصفحة من الذاكرة بحالتها القديمة — نظّف دائماً
    window.addEventListener('pageshow', hideBar);

    if (cancelBtn) {
      cancelBtn.addEventListener('click', function () {
        window.stop();
        hideBar();
      });
    }

    form.addEventListener('submit', function (e) {
      var grp = document.getElementById('group');
      if (grp && !grp.value) return;

      // نؤخّر الإرسال ~ثانية حتى يُرى شريط العد دائماً حتى مع الاستجابة الفورية
      e.preventDefault();
      showBar();
      submitTimer = setTimeout(function () { form.submit(); }, 1000);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
