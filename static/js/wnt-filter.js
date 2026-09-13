(function () {
  'use strict';

  var branchEl = document.getElementById('wnt-branch');
  var whEl = document.getElementById('wnt-wh');
  var dataEl = document.getElementById('wnt-wh-data');
  if (!branchEl || !whEl || !dataEl) return;

  var allWh = [];
  try {
    allWh = JSON.parse(dataEl.textContent || '[]');
  } catch (e) {
    allWh = [];
  }

  function fillWarehouses(branchCode, selectedCode) {
    var brn = String(branchCode || '').trim();
    var keep = String(selectedCode || '').trim();
    whEl.innerHTML = '';

    var placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = 'اختر مخزناً';
    whEl.appendChild(placeholder);

    var list = brn
      ? allWh.filter(function (w) {
          return String(w.branch_code || '') === brn;
        })
      : allWh.slice();

    if (!list.length) {
      var empty = document.createElement('option');
      empty.value = '';
      empty.textContent = brn ? 'لا مخازن لهذا الفرع' : 'لا مخازن متاحة';
      whEl.appendChild(empty);
      return;
    }

    list
      .slice()
      .sort(function (a, b) {
        return String(a.code).localeCompare(String(b.code), 'ar', {
          numeric: true,
        });
      })
      .forEach(function (w) {
        var opt = document.createElement('option');
        opt.value = w.code;
        opt.title = w.branch_name || w.name || w.code;
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
})();
