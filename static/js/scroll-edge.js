(function () {
  'use strict';

  var btn = document.getElementById('scroll-edge-btn');
  if (!btn) return;

  function innerScrollables() {
    var els = document.querySelectorAll('.ed-scroll, .table-wrap, .vt-table-wrap');
    var out = [];
    for (var i = 0; i < els.length; i++) {
      if (els[i].scrollHeight > els[i].clientHeight + 4) out.push(els[i]);
    }
    return out;
  }

  function pageHasScroll() {
    return document.documentElement.scrollHeight > window.innerHeight + 4;
  }

  function atBottom() {
    var docBottom =
      window.innerHeight + (window.scrollY || window.pageYOffset) >=
      document.documentElement.scrollHeight - 4;
    if (!docBottom && pageHasScroll()) return false;
    var inners = innerScrollables();
    for (var i = 0; i < inners.length; i++) {
      var el = inners[i];
      var inView = el.getBoundingClientRect();
      if (inView.bottom < 0 || inView.top > window.innerHeight) continue;
      if (el.scrollTop + el.clientHeight < el.scrollHeight - 4) return false;
    }
    return true;
  }

  function update() {
    if (!pageHasScroll() && innerScrollables().length === 0) {
      btn.hidden = true;
      return;
    }
    btn.hidden = false;
    var goDown = !atBottom();
    btn.classList.toggle('is-up', !goDown);
    var label = goDown ? 'النزول للأسفل' : 'الصعود للأعلى';
    btn.setAttribute('aria-label', label);
    btn.title = label;
  }

  btn.addEventListener('click', function () {
    var goDown = !atBottom();
    var inners = innerScrollables();
    for (var i = 0; i < inners.length; i++) {
      inners[i].scrollTo({
        top: goDown ? inners[i].scrollHeight : 0,
        behavior: 'smooth',
      });
    }
    window.scrollTo({
      top: goDown ? document.documentElement.scrollHeight : 0,
      behavior: 'smooth',
    });
  });

  window.addEventListener('scroll', update, { passive: true });
  window.addEventListener('resize', update);
  document.addEventListener('scroll', update, true);
  window.addEventListener('load', update);
  update();
})();
