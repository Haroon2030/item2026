(function () {
  'use strict';

  var HOST = '.page.turn-dash .table-wrap';
  var SKIP = 'a, button, input, select, textarea, label';
  var SLOP = 4;

  function canPan(el) {
    return el.scrollWidth > el.clientWidth + 1;
  }

  function bind(el) {
    if (el.dataset.panInit === '1') return;
    el.dataset.panInit = '1';
    var drag = null;

    function refresh() {
      el.classList.toggle('is-pannable', canPan(el));
    }

    refresh();
    window.addEventListener('resize', refresh);

    el.addEventListener('pointerdown', function (ev) {
      if (ev.button !== 0) return;
      if (ev.target.closest(SKIP)) return;
      if (!canPan(el)) return;
      drag = {
        x: ev.clientX,
        left: el.scrollLeft,
        moved: false,
        id: ev.pointerId,
      };
    });

    el.addEventListener('pointermove', function (ev) {
      if (!drag || ev.pointerId !== drag.id) return;
      var dx = ev.clientX - drag.x;
      if (!drag.moved && Math.abs(dx) < SLOP) return;
      if (!drag.moved) {
        drag.moved = true;
        el.classList.add('is-panning');
        try {
          if (el.setPointerCapture) el.setPointerCapture(ev.pointerId);
        } catch (err) {
          /* المؤشر قد لا يكون قابلاً للالتقاط */
        }
      }
      el.scrollLeft = drag.left - dx;
    });

    function end(ev) {
      if (!drag || (ev && ev.pointerId !== drag.id)) return;
      var moved = drag.moved;
      drag = null;
      el.classList.remove('is-panning');
      if (!moved) return;
      el.addEventListener('click', function stop(e) {
        e.preventDefault();
        e.stopPropagation();
        el.removeEventListener('click', stop, true);
      }, true);
    }

    el.addEventListener('pointerup', end);
    el.addEventListener('pointercancel', end);
  }

  function scan(root) {
    var scope = root && root.querySelectorAll ? root : document;
    if (scope instanceof Element && scope.matches(HOST)) bind(scope);
    scope.querySelectorAll(HOST).forEach(bind);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { scan(document); });
  } else {
    scan(document);
  }
})();
