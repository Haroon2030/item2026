(function () {
  'use strict';

  var scrollBox = document.getElementById('pr-returns-scroll');
  var table = scrollBox ? scrollBox.querySelector('.pr-returns-table') : null;
  var tbody = document.getElementById('pr-returns-body');
  var loader = document.getElementById('pr-loader');
  var doneNote = document.getElementById('pr-done');
  var countEl = document.getElementById('pr-loaded-count');
  if (!scrollBox || !table || !tbody) return;

  var api = table.getAttribute('data-api');
  var total = parseInt(table.getAttribute('data-total') || '0', 10);
  var pageSize = parseInt(table.getAttribute('data-page-size') || '50', 10);
  var loaded = tbody.querySelectorAll('tr').length;
  var loading = false;
  var finished = loaded >= total;

  function esc(text) {
    var div = document.createElement('div');
    div.textContent = text == null ? '' : String(text);
    return div.innerHTML;
  }

  function buildRow(row, index) {
    var tr = document.createElement('tr');
    tr.innerHTML =
      '<td class="mono">' + index + '</td>' +
      '<td class="mono pr-doc">' + esc(row.doc_date) +
        ' <small class="mono">#' + esc(row.doc_no) + '</small></td>' +
      '<td class="pr-branch" title="' + esc(row.branch_name) + '">' +
        esc(row.branch_code) + ' — ' + esc(row.branch_name) + '</td>' +
      '<td class="pr-item" title="' + esc(row.item_name) + '">' +
        esc(row.item_name) +
        ' <small class="mono">#' + esc(row.item_code) + '</small></td>' +
      '<td class="pr-group" title="' + esc(row.group_name) + '">' + esc(row.group_name) + '</td>' +
      '<td class="mono">' + esc(row.qty_display) + '</td>' +
      '<td class="mono pr-col-amt">' + esc(row.amount_display) + '</td>';
    return tr;
  }

  function setLoading(on) {
    loading = on;
    if (loader) loader.hidden = !on;
  }

  function finish() {
    finished = true;
    if (doneNote) doneNote.hidden = false;
  }

  function loadMore() {
    if (loading || finished) return;
    setLoading(true);
    var params = new URLSearchParams({
      date_from: table.getAttribute('data-date-from') || '',
      date_to: table.getAttribute('data-date-to') || '',
      branch: table.getAttribute('data-branch') || '',
      group: table.getAttribute('data-group') || '',
      vendor: table.getAttribute('data-vendor') || '',
      q: table.getAttribute('data-q') || '',
      offset: String(loaded),
      limit: String(pageSize),
    });
    fetch(api + '?' + params.toString(), { headers: { 'X-Requested-With': 'fetch' } })
      .then(function (resp) { return resp.json(); })
      .then(function (data) {
        if (!data.ok || !data.rows || !data.rows.length) {
          finish();
          return;
        }
        data.rows.forEach(function (row) {
          loaded += 1;
          tbody.appendChild(buildRow(row, loaded));
        });
        if (countEl) countEl.textContent = String(loaded);
        if (loaded >= total || data.rows.length < pageSize) finish();
      })
      .catch(function () { finish(); })
      .finally(function () { setLoading(false); });
  }

  function onScroll() {
    if (finished || loading) return;
    var rect = table.getBoundingClientRect();
    var nearBottom = rect.bottom <= window.innerHeight + 200;
    if (nearBottom) loadMore();
  }

  window.addEventListener('scroll', onScroll, { passive: true });
  window.addEventListener('resize', onScroll);
  if (finished && doneNote) doneNote.hidden = false;
})();

(function () {
  'use strict';

  var wrap = document.getElementById('pr-vendors-wrap');
  var table = document.getElementById('pr-vendors-table');
  if (!wrap || !table) return;
  var tbody = table.querySelector('tbody');
  var rows = tbody ? Array.prototype.slice.call(tbody.querySelectorAll('tr.pr-vendor-row')) : [];
  var countEl = document.getElementById('pr-vendors-loaded');
  var doneNote = document.getElementById('pr-vendors-done');
  var BATCH = 20;
  var shown = Math.min(BATCH, rows.length);

  rows.forEach(function (row, idx) {
    if (idx >= shown) row.classList.add('pr-hidden');
  });

  function go(row) {
    var href = row.getAttribute('data-href');
    if (href) window.location.href = href;
  }

  rows.forEach(function (row) {
    row.addEventListener('click', function () { go(row); });
    row.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' || ev.key === ' ') {
        ev.preventDefault();
        go(row);
      }
    });
  });

  function reveal() {
    var target = Math.min(shown + BATCH, rows.length);
    for (var i = shown; i < target; i += 1) rows[i].classList.remove('pr-hidden');
    shown = target;
    if (countEl) countEl.textContent = String(shown);
    if (shown >= rows.length && doneNote) doneNote.hidden = false;
  }

  function onScroll() {
    if (shown >= rows.length) return;
    if (wrap.scrollTop + wrap.clientHeight >= wrap.scrollHeight - 250) reveal();
  }

  wrap.addEventListener('scroll', onScroll, { passive: true });
  if (shown >= rows.length && doneNote) doneNote.hidden = rows.length === 0;
})();
