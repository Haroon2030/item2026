/**
 * تصفية قائمة المخازن حسب الفرع دون إرسال النموذج.
 * الاستخدام:
 *   BranchWhCascade.bind({
 *     branch: '#pr-branch',
 *     warehouse: '#pr-warehouse',
 *     data: '#pr-wh-data',
 *     requireBranch: true,
 *     blankNeedBranch: 'اختر الفرع أولاً',
 *     blankAll: 'كل المخازن',
 *   });
 */
(function (global) {
  'use strict';

  function normCode(value) {
    var s = String(value || '').trim();
    if (!s) return '';
    if (/^\d+$/.test(s)) {
      try {
        return String(parseInt(s, 10));
      } catch (err) {
        return s;
      }
    }
    return s;
  }

  function parseData(el) {
    if (!el) return [];
    try {
      return JSON.parse(el.textContent || '[]') || [];
    } catch (err) {
      return [];
    }
  }

  function qs(sel) {
    if (!sel) return null;
    if (sel.nodeType === 1) return sel;
    return document.querySelector(sel);
  }

  function fillWarehouses(opts, selectedCode) {
    var branchEl = opts.branchEl;
    var whEl = opts.whEl;
    var allWh = opts.allWh;
    var brn = String(branchEl.value || '').trim();
    var brnNorm = normCode(brn);
    var keep = String(selectedCode || '').trim();

    whEl.innerHTML = '';
    whEl.disabled = !!(opts.requireBranch && !brn);

    var placeholder = document.createElement('option');
    placeholder.value = '';
    if (!brn && opts.requireBranch) {
      placeholder.textContent = opts.blankNeedBranch || 'اختر الفرع أولاً';
    } else if (!brn) {
      placeholder.textContent = opts.blankNoBranch || opts.blankAll || 'كل المخازن';
    } else {
      placeholder.textContent = opts.blankAll || 'كل المخازن';
    }
    whEl.appendChild(placeholder);

    if (opts.requireBranch && !brn) {
      return;
    }

    var list = brn
      ? allWh.filter(function (w) {
          var wb = String(w.branch_code || '').trim();
          return wb === brn || normCode(wb) === brnNorm;
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
        return String(a.code || '').localeCompare(String(b.code || ''), 'ar', {
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

    if (keep && String(whEl.value) !== keep) {
      whEl.value = '';
    }

    if (typeof global.invSelectRefresh === 'function') {
      global.invSelectRefresh(whEl);
    }
  }

  function bind(options) {
    var opts = options || {};
    var branchEl = qs(opts.branch);
    var whEl = qs(opts.warehouse);
    var dataEl = qs(opts.data);
    if (!branchEl || !whEl || !dataEl) return null;

    var state = {
      branchEl: branchEl,
      whEl: whEl,
      allWh: parseData(dataEl),
      requireBranch: !!opts.requireBranch,
      blankNeedBranch: opts.blankNeedBranch || 'اختر الفرع أولاً',
      blankAll: opts.blankAll || 'كل المخازن',
      blankNoBranch: opts.blankNoBranch || '',
    };

    branchEl.addEventListener('change', function () {
      fillWarehouses(state, '');
    });

    return state;
  }

  function bindPair(options) {
    var opts = options || {};
    var dataEl = qs(opts.data);
    var allWh = parseData(dataEl);
    if (!dataEl) return;

    function bindSide(branchSel, whSel, blankAll, blankNoBranch) {
      var branchEl = qs(branchSel);
      var whEl = qs(whSel);
      if (!branchEl || !whEl) return;
      var state = {
        branchEl: branchEl,
        whEl: whEl,
        allWh: allWh,
        requireBranch: false,
        blankAll: blankAll || 'كل مخازن الفرع',
        blankNoBranch: blankNoBranch || 'كل المخازن',
      };
      branchEl.addEventListener('change', function () {
        fillWarehouses(state, '');
      });
    }

    bindSide(
      opts.branchFrom,
      opts.warehouseFrom,
      opts.blankFromBranch,
      opts.blankFromAll
    );
    bindSide(
      opts.branchTo,
      opts.warehouseTo,
      opts.blankToBranch,
      opts.blankToAll
    );
  }

  global.BranchWhCascade = {
    bind: bind,
    bindPair: bindPair,
    normCode: normCode,
  };
})(window);
