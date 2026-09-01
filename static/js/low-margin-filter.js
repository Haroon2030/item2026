(function () {
  'use strict';

  var branchEl = document.getElementById('lm-branch');
  var whEl = document.getElementById('lm-wh');
  var dataEl = document.getElementById('lm-wh-data');
  var formEl = document.getElementById('lm-filter-form');
  var minEl = document.getElementById('lm-min');
  var maxEl = document.getElementById('lm-max');
  if (!branchEl || !whEl || !dataEl) return;

  var allWh = [];
  try {
    allWh = JSON.parse(dataEl.textContent || '[]');
  } catch (e) {
    allWh = [];
  }

  function normalizeDecimalInput(el) {
    if (!el) return;
    var raw = String(el.value || '').trim().replace(/\u066C/g, ',').replace(/,/g, '.');
    if (!raw) return;
    var n = Number(raw);
    if (!Number.isFinite(n)) return;
    if (Math.abs(n - Math.round(n)) < 1e-9) {
      el.value = String(Math.round(n));
    } else {
      el.value = String(n);
    }
  }

  if (formEl) {
    formEl.addEventListener('submit', function () {
      normalizeDecimalInput(minEl);
      normalizeDecimalInput(maxEl);
    });
  }
  if (minEl) {
    minEl.addEventListener('blur', function () {
      normalizeDecimalInput(minEl);
    });
  }
  if (maxEl) {
    maxEl.addEventListener('blur', function () {
      normalizeDecimalInput(maxEl);
    });
  }

  function fillWarehouses(branchCode, selectedCode) {
    var brn = String(branchCode || '').trim();
    whEl.innerHTML = '';
    whEl.disabled = false;

    var allOpt = document.createElement('option');
    allOpt.value = '';
    allOpt.textContent = brn ? 'كل مخازن الفرع' : 'كل المخازن';
    whEl.appendChild(allOpt);

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
        return String(a.code).localeCompare(String(b.code), 'ar', { numeric: true });
      })
      .forEach(function (w) {
        var opt = document.createElement('option');
        opt.value = w.code;
        opt.title = w.name || w.code;
        opt.textContent = w.code + ' — ' + (w.name || w.code);
        if (selectedCode && String(selectedCode) === String(w.code)) {
          opt.selected = true;
        }
        whEl.appendChild(opt);
      });
  }

  branchEl.addEventListener('change', function () {
    fillWarehouses(branchEl.value, '');
  });
})();
