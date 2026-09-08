(function () {
  'use strict';

  var BTN =
    '.js-fit-print, .lm-pdf-btn, .up-pdf-btn, .wh-out-pdf-btn, .wh-exp-print-btn, .inv-pack-err-print-btn';

  function prepare(scope) {
    document.body.classList.add('fit-printing');
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
    }
  });
  window.addEventListener('afterprint', cleanup);
})();
