(function () {
  'use strict';

  var BTN =
    '.js-fit-print, .dash-pdf-btn, .lm-pdf-btn, .up-pdf-btn, .wh-out-pdf-btn, .wh-exp-print-btn, .inv-pack-err-print-btn, .vpc-print-btn, .wqc-pdf-btn';
  var STYLE_ID = 'fit-print-page-style';
  var TABLE_SEL =
    'table.data-table, table.sales-table, table.lm-table, table.up-table, table.nbc-table, table.wh-out-table, table.wh-exp-table, table.inv-pack-err-table, table.wqc-table, table.vt-sheet, table.vt-table, table.perf-compare-table, table.purchase-table, table.income-table, table.suppliers-table, table.assets-table';
  /* A4 landscape usable width ≈ 297mm − 12mm margins */
  var PAGE_WIDTH_PX = Math.round((297 - 12) * (96 / 25.4));
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
      '@media print { @page { size: A4 landscape; margin: 6mm; } }';
  }

  function clearPrintTargets() {
    document.querySelectorAll('.is-print-target, .is-print-ancestor').forEach(function (el) {
      el.classList.remove('is-print-target');
      el.classList.remove('is-print-ancestor');
    });
  }

  function clearTableFit() {
    document.body.classList.remove('fit-measuring');
    document.querySelectorAll('table[data-fit-print-fs], table[data-fit-scale]').forEach(function (table) {
      table.removeAttribute('data-fit-print-fs');
      table.removeAttribute('data-fit-measuring');
      table.removeAttribute('data-fit-scale');
      table.style.removeProperty('--fit-print-fs');
      table.style.removeProperty('--fit-print-scale');
      table.style.removeProperty('font-size');
      table.style.removeProperty('width');
      table.style.removeProperty('table-layout');
      table.style.removeProperty('max-width');
      table.style.removeProperty('zoom');
    });
  }

  function collectTables(root) {
    var out = [];
    if (!root) return out;
    root.querySelectorAll('table').forEach(function (table) {
      if (table.closest('[hidden], .is-collapsed')) return;
      out.push(table);
    });
    return out;
  }

  function tablesForButton(btn) {
    var panel = btn.closest('.dash-panel, .wqc-sheet, .vt-sheet');
    if (panel) {
      var inPanel = collectTables(panel);
      if (inPanel.length) return inPanel;
    }
    var section = btn.closest('section, article');
    if (section && !section.classList.contains('dash-head')) {
      var inSection = collectTables(section);
      if (inSection.length) return inSection;
    }
    return collectTables(document.querySelector('main') || document.body);
  }

  function markTables(tables) {
    (tables || []).forEach(function (table) {
      var host = table.closest('.table-wrap, [class*="-scroll"]') || table;
      if (host.classList.contains('is-print-target')) return;
      host.classList.add('is-print-target');
      var el = host.parentElement;
      while (el && el !== document.body) {
        el.classList.add('is-print-ancestor');
        el = el.parentElement;
      }
    });
    document.body.classList.add('fit-table-only');
    document.body.setAttribute('data-print-scope', 'table');
  }

  function fitVisibleTables() {
    clearTableFit();
    var roots = document.querySelectorAll('.is-print-target');
    var tables = [];
    if (roots.length) {
      roots.forEach(function (root) {
        if (root.matches('table')) {
          tables.push(root);
          return;
        }
        root.querySelectorAll('table').forEach(function (table) {
          tables.push(table);
        });
      });
    } else {
      (document.querySelector('main') || document.body)
        .querySelectorAll(TABLE_SEL + ', .table-wrap > table, [class*="-scroll"] > table')
        .forEach(function (table) {
          tables.push(table);
        });
    }
    var seen = [];
    document.body.classList.add('fit-measuring');
    void document.body.offsetHeight;

    tables.forEach(function (table) {
      if (seen.indexOf(table) !== -1) return;
      seen.push(table);
      if (table.closest('[hidden], .is-collapsed')) return;

      table.setAttribute('data-fit-print-fs', '1');
      table.setAttribute('data-fit-measuring', '1');
      var natural = table.scrollWidth || table.offsetWidth || 0;
      table.removeAttribute('data-fit-measuring');
      if (natural > PAGE_WIDTH_PX && natural > 0) {
        var scale = PAGE_WIDTH_PX / natural;
        table.setAttribute('data-fit-scale', '1');
        table.style.setProperty('--fit-print-scale', String(scale));
        table.style.zoom = String(scale);
      }
    });
    document.body.classList.remove('fit-measuring');
  }

  function prepare(tables) {
    ensureLandscapeStyle();
    clearPrintTargets();
    clearTableFit();
    document.body.classList.add('fit-printing');
    document.body.classList.add('print-landscape');
    if (!tables || !tables.length) {
      tables = collectTables(document.querySelector('main') || document.body);
    }
    markTables(tables);
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
    document.body.classList.remove('fit-table-only');
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
    prepare(tablesForButton(btn));
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
      prepare(collectTables(document.querySelector('main') || document.body));
    } else {
      ensureLandscapeStyle();
    }
  });
  window.addEventListener('afterprint', cleanup);

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
