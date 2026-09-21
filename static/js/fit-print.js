(function () {
  'use strict';

  var BTN =
    '.js-fit-print, .dash-pdf-btn, .lm-pdf-btn, .up-pdf-btn, .wh-out-pdf-btn, .wh-exp-print-btn, .inv-pack-err-print-btn, .pos-unavl-print-btn, .vpc-print-btn, .wqc-pdf-btn';
  var STYLE_ID = 'fit-print-page-style';
  var TABLE_SEL =
    'table.data-table, table.sales-table, table.lm-table, table.up-table, table.nbc-table, table.wh-out-table, table.wh-exp-table, table.inv-pack-err-table, table.wqc-table, table.vt-sheet, table.vt-table, table.perf-compare-table, table.purchase-table, table.income-table, table.suppliers-table';
  /* A4 landscape usable width ≈ 297mm − 8mm margins */
  var PAGE_WIDTH_PX = Math.round((297 - 8) * (96 / 25.4));
  var MAX_FS = 10;
  var MIN_FS = 5.5;
  var PDF_ICON =
    '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5M9 13h6M9 17h4"/></svg>';

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

  function clearPrintTargets() {
    document.querySelectorAll('.is-print-target, .is-print-ancestor').forEach(function (el) {
      el.classList.remove('is-print-target');
      el.classList.remove('is-print-ancestor');
    });
  }

  function markPanelOnly(panel) {
    if (!panel) return;
    panel.classList.add('is-print-target');
    var el = panel.parentElement;
    while (el && el !== document.body) {
      el.classList.add('is-print-ancestor');
      el = el.parentElement;
    }
  }

  function clearTableFit() {
    document.querySelectorAll('table[data-fit-print-fs]').forEach(function (table) {
      table.removeAttribute('data-fit-print-fs');
      table.removeAttribute('data-fit-measuring');
      table.style.removeProperty('--fit-print-fs');
      table.style.removeProperty('font-size');
      table.style.removeProperty('width');
      table.style.removeProperty('table-layout');
      table.style.removeProperty('max-width');
    });
  }

  function printRoot() {
    return (
      document.querySelector('.is-print-target') ||
      document.querySelector('main') ||
      document.body
    );
  }

  function fitVisibleTables() {
    clearTableFit();
    var root = printRoot();
    var tables = root.querySelectorAll(TABLE_SEL);
    var avail = PAGE_WIDTH_PX;

    tables.forEach(function (table) {
      if (table.closest('[hidden], .is-collapsed')) return;

      table.setAttribute('data-fit-print-fs', '1');
      table.setAttribute('data-fit-measuring', '1');
      table.style.setProperty('--fit-print-fs', MAX_FS + 'px');

      var natural = table.scrollWidth || table.offsetWidth || 0;
      var fs = MAX_FS;
      if (natural > avail && natural > 0) {
        fs = Math.max(MIN_FS, (MAX_FS * avail) / natural);
      }
      table.style.setProperty('--fit-print-fs', fs.toFixed(2) + 'px');
      table.removeAttribute('data-fit-measuring');
    });
  }

  function prepare(scope, panel) {
    ensureLandscapeStyle();
    clearPrintTargets();
    clearTableFit();
    document.body.classList.add('fit-printing');
    document.body.classList.add('print-landscape');
    if (scope === 'panel' && panel) {
      markPanelOnly(panel);
      document.body.setAttribute('data-print-scope', 'panel');
    } else if (scope) {
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
    fitVisibleTables();
  }

  function cleanup() {
    document.body.classList.remove('fit-printing');
    document.body.classList.remove('print-landscape');
    document.body.removeAttribute('data-print-scope');
    clearPrintTargets();
    clearTableFit();
  }

  function ensurePanelPdfButtons() {
    document.querySelectorAll('.dash-panel').forEach(function (panel) {
      if (!panel.querySelector('table')) return;
      var head = panel.querySelector(':scope > .dash-panel-head');
      if (!head) head = panel.querySelector('.dash-panel-head');
      if (!head) return;
      if (head.querySelector('.dash-pdf-btn, .js-fit-print, .vpc-print-btn, .lm-pdf-btn, .up-pdf-btn')) {
        return;
      }

      var actions = head.querySelector('.dash-panel-head-actions');
      if (!actions) {
        actions = document.createElement('div');
        actions.className = 'dash-panel-head-actions';
        head.appendChild(actions);
      }

      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'dash-pdf-btn dash-pdf-btn-sm js-fit-print';
      btn.setAttribute('data-print-scope', 'panel');
      btn.setAttribute('title', 'حفظ هذا الجدول PDF');
      btn.innerHTML = PDF_ICON + ' PDF';
      actions.appendChild(btn);
    });
  }

  function onClick(ev) {
    var btn = ev.target.closest(BTN);
    if (!btn) return;
    ev.preventDefault();
    var scope = btn.getAttribute('data-print-scope') || '';
    var panel = null;
    if (scope === 'panel') {
      panel = btn.closest('.dash-panel');
      if (!panel) return;
    }
    prepare(scope, panel);
    window.setTimeout(function () {
      fitVisibleTables();
      window.print();
    }, 40);
  }

  function boot() {
    ensurePanelPdfButtons();
  }

  document.addEventListener('click', onClick);
  window.addEventListener('beforeprint', function () {
    if (!document.body.classList.contains('fit-printing')) {
      prepare('');
    } else {
      ensureLandscapeStyle();
      fitVisibleTables();
    }
  });
  window.addEventListener('afterprint', cleanup);

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
