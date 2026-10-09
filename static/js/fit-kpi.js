(function () {
  function overflows(el, mv, tx) {
    // بلا هامش تسامح: تجاوز بكسل واحد يكفي ليستبدل المتصفح آخر خانات الرقم بـ «…» (فيضيع 2–3 أرقام)
    return (
      mv.scrollWidth > mv.clientWidth ||
      el.scrollWidth > el.clientWidth ||
      (tx && tx.scrollWidth > tx.clientWidth) ||
      (tx && mv.getBoundingClientRect().width > el.clientWidth)
    );
  }
  function fit(el) {
    var mv = el.querySelector('.money') || el;
    var tx = el.querySelector('.money-value');
    el.style.removeProperty('font-size');
    var base = parseFloat(getComputedStyle(el).fontSize);
    var size = base;
    var min = 9;
    while (size > min && overflows(el, mv, tx)) {
      size -= 0.5;
      el.style.setProperty('font-size', size + 'px', 'important');
    }
    // scrollWidth/clientWidth أعداد صحيحة: قد يبقى كسر بكسل زائد لا يُقاس، فنزيد خطوة أمان بعد أي تصغير
    if (size < base) {
      el.style.setProperty('font-size', Math.max(min, size - 0.75) + 'px', 'important');
    }
  }
  function run() {
    document.querySelectorAll('.sales-ov-kpis .dash-kpi-value').forEach(fit);
  }
  var t;
  function later() { clearTimeout(t); t = setTimeout(run, 80); }
  window.addEventListener('load', run);
  window.addEventListener('resize', later);
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(run);
  // خط الأرقام قد يُحمَّل متأخراً (أو يتبدّل بخط احتياطي أعرض) بعد أول قياس فيتّسع النص ويُبتر بـ «…»:
  // نعيد القياس كلما انتهى تحميل خط، ومرتين بعد التحميل كاحتياط.
  if (document.fonts && document.fonts.addEventListener) document.fonts.addEventListener('loadingdone', later);
  window.addEventListener('load', function () { setTimeout(run, 600); setTimeout(run, 2500); });

  // تغيّر عرض البطاقة دون تغيّر النافذة (طيّ/فتح القائمة الجانبية، تغيّر مساحة الصفحة) لا يُطلق resize:
  // فكان الرقم المقلَّص سابقاً يبقى بحجمه القديم ويظهر مبتوراً «…3,140,203.17». نراقب عرض كل بطاقة.
  if (typeof ResizeObserver !== 'undefined') {
    var widths = new WeakMap();
    var ro = new ResizeObserver(function (entries) {
      var changed = false;
      entries.forEach(function (e) {
        var w = Math.round(e.contentRect.width);
        if (widths.get(e.target) !== w) {
          widths.set(e.target, w);
          changed = true;
        }
      });
      if (changed) later();
    });
    var watch = function () {
      document.querySelectorAll('.sales-ov-kpis .dash-kpi-card').forEach(function (card) { ro.observe(card); });
    };
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', watch);
    else watch();
  }
})();
