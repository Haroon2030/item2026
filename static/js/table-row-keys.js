(function () {
  'use strict';

  /** @type {{ tbody: HTMLTableSectionElement | null, row: HTMLTableRowElement | null, scrollHost: HTMLElement | null }} */
  var active = { tbody: null, row: null, scrollHost: null };

  var SCROLL_HOST_SEL =
    '.table-wrap, .vt-table-wrap, .vt-sheet-wrap, .assets-list-wrap, ' +
    '.suppliers-scroll, .pr-vendors-wrap, .pr-compare-sheet-wrap, ' +
    '.users-table-wrap, .wh-exp-scroll, .inv-pack-err-scroll, .sales-ov-scroll, ' +
    '.lm-scroll, .wh-out-scroll, .income-top-scroll';

  var SKIP_TABLE_SEL =
    '.inv-table-foot, .income-account-foot-table, [data-no-row-keys]';

  function isFormField(el) {
    if (!el || !el.tagName) return false;
    var tag = el.tagName;
    return tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA' || tag === 'BUTTON';
  }

  function isEmptyRow(tr) {
    var cell = tr.querySelector('td[colspan], th[colspan]');
    return !!cell;
  }

  function isNavigableRow(tr) {
    if (!tr || tr.parentElement.tagName !== 'TBODY') return false;
    if (isEmptyRow(tr)) return false;
    if (tr.classList.contains('pr-hidden') && tr.offsetParent === null) return false;
    var table = tr.closest('table');
    if (!table || table.matches(SKIP_TABLE_SEL)) return false;
    return true;
  }

  function scrollHostFor(table) {
    if (!table) return null;
    var host = table.closest(SCROLL_HOST_SEL);
    if (host) return host;
    var parent = table.parentElement;
    return parent instanceof HTMLElement ? parent : null;
  }

  function rowsIn(tbody) {
    return Array.prototype.filter.call(tbody.querySelectorAll('tr'), isNavigableRow);
  }

  function ensureFocusable(host) {
    if (!host || host.hasAttribute('tabindex')) return;
    host.setAttribute('tabindex', '0');
  }

  function selectRow(tbody, row, opts) {
    if (!isNavigableRow(row) || row.parentElement !== tbody) return;
    if (active.row) active.row.classList.remove('is-selected');
    var host = scrollHostFor(row.closest('table'));
    active = { tbody: tbody, row: row, scrollHost: host };
    row.classList.add('is-selected');
    if (!opts || opts.scroll !== false) {
      row.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    }
    if (host) {
      ensureFocusable(host);
      host.focus({ preventScroll: true });
    }
  }

  function move(delta) {
    if (!active.tbody) return;
    var list = rowsIn(active.tbody);
    if (!list.length) return;
    var idx = active.row ? list.indexOf(active.row) : -1;
    if (idx < 0) idx = delta > 0 ? -1 : 0;
    var next = Math.max(0, Math.min(list.length - 1, idx + delta));
    selectRow(active.tbody, list[next]);
    if (next >= list.length - 5 && active.scrollHost) {
      active.scrollHost.dispatchEvent(new Event('scroll'));
    }
  }

  function onKeydown(ev) {
    if (!active.tbody || !active.row) return;
    if (isFormField(ev.target)) return;
    if (ev.key === 'ArrowDown') {
      ev.preventDefault();
      move(1);
    } else if (ev.key === 'ArrowUp') {
      ev.preventDefault();
      move(-1);
    } else if (ev.key === 'Home') {
      ev.preventDefault();
      var first = rowsIn(active.tbody)[0];
      if (first) selectRow(active.tbody, first);
    } else if (ev.key === 'End') {
      ev.preventDefault();
      var list = rowsIn(active.tbody);
      if (list.length) selectRow(active.tbody, list[list.length - 1]);
    }
  }

  function initTable(table) {
    if (!(table instanceof HTMLTableElement)) return;
    if (table.dataset.rowKeysInit === '1') return;
    if (table.matches(SKIP_TABLE_SEL)) return;
    var tbody = table.querySelector('tbody');
    if (!tbody) return;
    if (!rowsIn(tbody).length) return;

    table.dataset.rowKeysInit = '1';
    var host = scrollHostFor(table);
    if (host) ensureFocusable(host);

    tbody.addEventListener('click', function (ev) {
      var row = ev.target.closest('tr');
      if (!isNavigableRow(row) || row.parentElement !== tbody) return;
      selectRow(tbody, row, { scroll: false });
    });

    if (host) {
      host.addEventListener('keydown', onKeydown);
    }

    new MutationObserver(function () {
      if (active.row && active.tbody === tbody && !tbody.contains(active.row)) {
        if (active.row) active.row.classList.remove('is-selected');
        active = { tbody: null, row: null, scrollHost: null };
      }
    }).observe(tbody, { childList: true });
  }

  function scan(root) {
    if (root instanceof HTMLTableElement) {
      initTable(root);
      return;
    }
    var scope = root && root.querySelectorAll ? root : document;
    scope.querySelectorAll('table').forEach(initTable);
  }

  document.addEventListener('keydown', onKeydown);
  scan(document);

  document.addEventListener('DOMContentLoaded', function () {
    scan(document);
  });

  document.addEventListener('lm-rows-added', function () {
    var tbody = document.getElementById('lm-body');
    if (tbody) scan(tbody.closest('table'));
  });

  var scanTimer = 0;
  var bodyObserver = new MutationObserver(function (mutations) {
    var needsScan = false;
    mutations.forEach(function (m) {
      if (m.addedNodes && m.addedNodes.length) needsScan = true;
    });
    if (!needsScan) return;
    window.clearTimeout(scanTimer);
    scanTimer = window.setTimeout(function () {
      scan(document);
    }, 120);
  });
  bodyObserver.observe(document.body, { childList: true, subtree: true });
})();
