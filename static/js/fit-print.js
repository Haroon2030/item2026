(function () {
  'use strict';

  var BTN =
    '.js-fit-print, .lm-pdf-btn, .up-pdf-btn, .wh-out-pdf-btn, .wh-exp-print-btn, .inv-pack-err-print-btn, .pos-unavl-print-btn, .vpc-print-btn';
  var STYLE_ID = 'fit-print-page-style';

  function ensureLandscapeStyle() {
    var el = document.getElementById(STYLE_ID);
    if (!el) {
      el = document.createElement('style');
      el.id = STYLE_ID;
      document.head.appendChild(el);
    }
    el.textContent =
      '@media print { @page { size: A4 landscape; margin: 4mm; } }';
  }

  function prepare(scope) {
    ensureLandscapeStyle();
    document.body.classList.add('fit-printing');
    document.body.classList.add('print-landscape');
    if (scope) {
      document.body.setAttribute('data-print-scope', scope);
    } else {
      document.body.removeAttribute('data-print-scope');
    }
    document.querySelectorAll(
      '.table-wrap, [class*="-scroll"], #lm-wrap'
    ).forEach(function (el) {
      try {
        el.scrollLeft = 0;
        el.scrollTop = 0;
      } catch (e) {}
    });
  }

  function cleanup() {
    document.body.classList.remove('fit-printing');
    document.body.classList.remove('print-landscape');
    document.body.removeAttribute('data-print-scope');
  }

  function onClick(ev) {
    var btn = ev.target.closest(BTN);
    if (!btn) return;
    ev.preventDefault();
    prepare(btn.getAttribute('data-print-scope') || '');
    window.setTimeout(function () {
      window.print();
    }, 30);
  }

  document.addEventListener('click', onClick);
  window.addEventListener('beforeprint', function () {
    if (!document.body.classList.contains('fit-printing')) {
      prepare('');
    } else {
      ensureLandscapeStyle();
    }
  });
  window.addEventListener('afterprint', cleanup);
})();
