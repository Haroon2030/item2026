(function () {
  'use strict';

  var branchEl = document.getElementById('cadj-branch');
  var whEl = document.getElementById('cadj-wh');
  var dataEl = document.getElementById('cadj-wh-data');
  if (branchEl && whEl && dataEl) {
    var allWh = [];
    try {
      allWh = JSON.parse(dataEl.textContent || '[]');
    } catch (e) {
      allWh = [];
    }

    function fillWarehouses(branchCode, selectedCode) {
      var brn = String(branchCode || '').trim();
      var keep = String(selectedCode || '').trim() || String(whEl.value || '').trim();
      whEl.innerHTML = '';

      var placeholder = document.createElement('option');
      placeholder.value = '';
      placeholder.textContent = 'كل المخازن';
      whEl.appendChild(placeholder);

      var list = brn
        ? allWh.filter(function (w) {
            return String(w.branch_code || '') === brn;
          })
        : allWh.slice();

      list
        .slice()
        .sort(function (a, b) {
          return String(a.code).localeCompare(String(b.code), 'ar', { numeric: true });
        })
        .forEach(function (w) {
          var opt = document.createElement('option');
          opt.value = w.code;
          opt.title = w.name || w.code;
          opt.textContent = w.code + ' — ' + (w.name || w.code);
          if (keep && String(keep) === String(w.code)) {
            opt.selected = true;
          }
          whEl.appendChild(opt);
        });
    }

    branchEl.addEventListener('change', function () {
      fillWarehouses(branchEl.value, '');
    });
  }

  document.addEventListener('click', function (ev) {
    var btn = ev.target.closest('[data-cadj-toggle]');
    if (!btn) return;
    var key = String(btn.getAttribute('data-cadj-toggle') || '').trim();
    if (!key) return;
    var detail = document.getElementById('cadj-lines-' + key);
    if (!detail) return;
    var open = detail.hasAttribute('hidden');
    if (open) {
      detail.removeAttribute('hidden');
      detail.classList.remove('is-collapsed');
      btn.setAttribute('aria-expanded', 'true');
      btn.closest('tr') && btn.closest('tr').classList.add('is-open');
    } else {
      detail.setAttribute('hidden', '');
      detail.classList.add('is-collapsed');
      btn.setAttribute('aria-expanded', 'false');
      btn.closest('tr') && btn.closest('tr').classList.remove('is-open');
    }
  });
})();
