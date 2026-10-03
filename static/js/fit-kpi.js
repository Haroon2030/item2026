(function () {
  function fit(el) {
    var mv = el.querySelector('.money') || el;
    var tx = el.querySelector('.money-value');
    el.style.removeProperty('font-size');
    var size = parseFloat(getComputedStyle(el).fontSize);
    var min = 9;
    while (size > min && (mv.scrollWidth > mv.clientWidth + 1 || el.scrollWidth > el.clientWidth + 1 || (tx && tx.scrollWidth > tx.clientWidth + 1) || (tx && mv.getBoundingClientRect().width > el.clientWidth + 1))) {
      size -= 0.5;
      el.style.setProperty('font-size', size + 'px', 'important');
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
})();
