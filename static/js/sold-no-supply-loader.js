(function () {
  'use strict';

  var wrap = document.getElementById('sns-wrap');
  var table = document.getElementById('sns-items-table');
  var tbody = document.getElementById('sns-items-body');
  var loader = document.getElementById('sns-loader');
  var countEl = document.getElementById('sns-loaded-count');
  if (!wrap || !table || !tbody) return;

  var api = table.getAttribute('data-api');
  var total = parseInt(table.getAttribute('data-total') || '0', 10);
  var pageSize = parseInt(table.getAttribute('data-page-size') || '80', 10);
  var loaded = tbody.querySelectorAll('tr:not(.sales-empty)').length;
  var loading = false;
  var finished = total <= 0 || loaded >= total;

  function esc(text) {
    var div = document.createElement('div');
    div.textContent = text == null ? '' : String(text);
    return div.innerHTML;
  }

  function buildRow(row, index) {
    var tr = document.createElement('tr');
    tr.innerHTML =
      '<td class="mono">' + index + '</td>' +
      '<td class="pr-item" title="' + esc(row.item_name) + '">' +
        esc(row.item_name) +
        ' <small class="mono">#' + esc(row.item_code) + '</small></td>' +
      '<td class="pr-group" title="' + esc(row.group_name) + '">' + esc(row.group_name) + '</td>' +
      '<td class="pr-branch" title="' + esc(row.branch_name) + '">' +
        esc(row.branch_code) + ' — ' + esc(row.branch_name) + '</td>' +
      '<td class="mono pr-col-amt">' + esc(row.qty_display) + '</td>';
    return tr;
  }

  function setLoading(on) {
    loading = on;
    if (loader) loader.hidden = !on;
  }

  function finish() {
    finished = true;
    if (loader) loader.hidden = true;
  }

  function loadMore() {
    if (loading || finished) return;
    setLoading(true);
    var params = new URLSearchParams({
      date_from: table.getAttribute('data-date-from') || '',
      date_to: table.getAttribute('data-date-to') || '',
      branch: table.getAttribute('data-branch') || '',
      group: table.getAttribute('data-group') || '',
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
    if (wrap.scrollTop + wrap.clientHeight >= wrap.scrollHeight - 250) loadMore();
  }

  wrap.addEventListener('scroll', onScroll, { passive: true });
})();
