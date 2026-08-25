(function () {
  'use strict';

  var wrap = document.getElementById('lm-wrap');
  var table = document.getElementById('lm-table');
  var tbody = document.getElementById('lm-body');
  var loader = document.getElementById('lm-loader');
  var countEl = document.getElementById('lm-loaded-count');
  if (!wrap || !table || !tbody) return;

  var api = table.getAttribute('data-api') || '';
  var total = parseInt(table.getAttribute('data-total') || '0', 10);
  var pageSize = parseInt(table.getAttribute('data-page-size') || '20', 10);
  var scrollMax = parseInt(table.getAttribute('data-scroll-max') || '2000', 10);
  var loaded = tbody.querySelectorAll('tr').length;
  var loading = false;
  var finished = total > 0 ? loaded >= Math.min(total, scrollMax) : loaded < pageSize;

  function esc(text) {
    var div = document.createElement('div');
    div.textContent = text == null ? '' : String(text);
    return div.innerHTML;
  }

  function rowClass(prft) {
    var n = Number(prft);
    if (n < 0) return 'is-loss';
    if (n < 5) return 'is-warn';
    if (n < 10) return 'is-mid';
    return 'is-ok';
  }

  function buildRow(row, index) {
    var tr = document.createElement('tr');
    var even = index % 2 === 0 ? ' is-even' : '';
    tr.className = rowClass(row.profit_pct) + even;
    tr.innerHTML =
      '<td class="mono lm-td-idx">' + index + '</td>' +
      '<td class="mono lm-td-code">' + esc(row.item_code) + '</td>' +
      '<td class="lm-td-item"><span class="lm-item-name" title="' + esc(row.item_name) + '">' +
        esc(row.item_name) + '</span></td>' +
      '<td class="lm-td-unit">' + esc(row.unit) + '</td>' +
      '<td class="lm-td-wh" title="' + esc(row.wh_name) + '"><strong class="mono">' +
        esc(row.wh_code) + '</strong></td>' +
      '<td class="mono lm-td-cost">' + esc(row.avg_cost_display) + '</td>' +
      '<td class="mono lm-td-price">' + esc(row.price_display) + '</td>' +
      '<td class="mono lm-td-prft">' + esc(row.profit_pct_display) + '%</td>' +
      '<td class="mono lm-td-cy">' + esc(row.currency) + '</td>' +
      '<td class="mono lm-td-lev">' + esc(row.lev_no) + '</td>' +
      '<td class="lm-td-levn">' + esc(row.lev_name) + '</td>';
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
    if (loading || finished || !api) return;
    if (loaded >= scrollMax) {
      finish();
      return;
    }
    setLoading(true);
    var params = new URLSearchParams({
      branch: table.getAttribute('data-branch') || '',
      warehouses: table.getAttribute('data-warehouses') || '',
      q: table.getAttribute('data-q') || '',
      max_profit: table.getAttribute('data-max-profit') || '15',
      lev: table.getAttribute('data-lev') || '1',
      include_neg: table.getAttribute('data-include-neg') || '1',
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
        if (!data.has_more || loaded >= scrollMax || data.rows.length < pageSize) {
          finish();
        }
      })
      .catch(function () { finish(); })
      .finally(function () { setLoading(false); });
  }

  function onScroll() {
    if (finished || loading) return;
    if (wrap.scrollTop + wrap.clientHeight >= wrap.scrollHeight - 120) loadMore();
  }

  wrap.addEventListener('scroll', onScroll, { passive: true });
})();
