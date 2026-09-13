(function () {
  'use strict';

  var dataEl = document.getElementById('pack-err-wh-data');
  var branchFromEl = document.getElementById('pack-err-branch-from');
  var branchToEl = document.getElementById('pack-err-branch-to');
  var whFromEl = document.getElementById('pack-err-wh-from');
  var whToEl = document.getElementById('pack-err-wh-to');
  if (!dataEl || !branchFromEl || !branchToEl || !whFromEl || !whToEl) return;

  var allWh = [];
  try {
    allWh = JSON.parse(dataEl.textContent || '[]');
  } catch (e) {
    allWh = [];
  }

  function allLabel(branchCode, side) {
    var brn = String(branchCode || '').trim();
    if (!brn) return 'كل المخازن';
    return side === 'to' ? 'كل مخازن فرع الوصول' : 'كل مخازن فرع المصدر';
  }

  function fillWarehouses(branchEl, whEl, side, selectedCode) {
    var brn = String(branchEl.value || '').trim();
    var keep = String(selectedCode || '').trim();
    whEl.innerHTML = '';

    var placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = allLabel(brn, side);
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
        opt.title = w.name || w.code;
        opt.textContent = w.code + ' — ' + (w.name || w.code);
        if (keep && String(keep) === String(w.code)) {
          opt.selected = true;
        }
        whEl.appendChild(opt);
      });
  }

  branchFromEl.addEventListener('change', function () {
    fillWarehouses(branchFromEl, whFromEl, 'from', '');
  });
  branchToEl.addEventListener('change', function () {
    fillWarehouses(branchToEl, whToEl, 'to', '');
  });
})();
