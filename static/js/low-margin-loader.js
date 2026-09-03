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
  var pageSize = parseInt(table.getAttribute('data-page-size') || '200', 10);
  var loaded = tbody.querySelectorAll('tr').length;
  var loading = false;
  var finished = table.getAttribute('data-has-more') !== '1';

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
    tr.className = rowClass(row.profit_pct);
    tr.innerHTML =
      '<td class="mono lm-td-idx">' + index + '</td>' +
      '<td class="mono lm-td-code">' + esc(row.item_code) + '</td>' +
      '<td class="lm-td-item" title="' + esc(row.item_name) + '">' + esc(row.item_name) + '</td>' +
      '<td class="mono lm-td-barcode" dir="ltr">' + esc(row.barcode || '') + '</td>' +
      '<td class="lm-td-unit">' + esc(row.unit) + '</td>' +
      '<td class="mono lm-td-wh" title="' + esc(row.wh_name) + '">' + esc(row.wh_code) + '</td>' +
      '<td class="lm-td-group" title="' + esc(row.g_code) + '">' + esc(row.g_name) + '</td>' +
      '<td class="mono lm-td-cost">' + esc(row.avg_cost_display) + '</td>' +
      '<td class="mono lm-td-price">' + esc(row.price_display) + '</td>' +
      '<td class="mono lm-td-prft" title="I_CWTAVG ' + esc(row.unit_cost_display) + '">' + esc(row.profit_pct_display) + '%</td>' +
      '<td class="mono lm-td-onixprft" title="' + esc(row.onyx_cost_src_label || 'متوسط') + ' ' + esc(row.primary_cost_display) + '">' + esc(row.onyx_profit_pct_display) + '%</td>' +
      '<td class="mono lm-td-limprft" title="' + esc(row.limit_source === 'min' ? 'من أقل سعر مسموح' : 'عند حد «إلى»') + '">' + esc(row.limit_profit_pct_display) + '%</td>' +
      '<td class="mono lm-td-limprc" title="' + esc(row.limit_source === 'min' ? 'أقل سعر مسموح' : 'سعر عند حد «إلى»') + '">' + esc(row.limit_price_display) + '</td>' +
      '<td class="mono lm-td-cy">' + esc(row.currency) + '</td>' +
      '<td class="mono lm-td-lev">' + esc(row.lev_no) + '</td>' +
      '<td class="lm-td-levn">' + esc(row.lev_name) + '</td>';
    return tr;
  }

  function setLoading(on, msg) {
    loading = on;
    if (!loader) return;
    loader.hidden = !on;
    if (msg) loader.textContent = msg;
    else if (on) loader.textContent = 'جاري تحميل المزيد…';
  }

  function finish() {
    finished = true;
    if (loader) loader.hidden = true;
  }

  function nearBottom() {
    return wrap.scrollTop + wrap.clientHeight >= wrap.scrollHeight - 160;
  }

  function needsFill() {
    return wrap.scrollHeight <= wrap.clientHeight + 12;
  }

  function canLoadMore() {
    if (loading || finished || !api) return false;
    if (total > 0 && loaded >= total) return false;
    return true;
  }

  function loadMore() {
    if (!canLoadMore()) {
      if (total > 0 && loaded >= total) finish();
      return;
    }
    setLoading(true);
    var params = new URLSearchParams({
      branch: table.getAttribute('data-branch') || '',
      warehouses: table.getAttribute('data-warehouses') || '',
      group: table.getAttribute('data-group') || '',
      q: table.getAttribute('data-q') || '',
      min_profit: table.getAttribute('data-min-profit') || '',
      max_profit: table.getAttribute('data-max-profit') || '15',
      lev: table.getAttribute('data-lev') || '1',
      include_neg: table.getAttribute('data-include-neg') || '1',
      offset: String(loaded),
      limit: String(pageSize),
    });
    fetch(api + '?' + params.toString(), {
      credentials: 'same-origin',
      headers: { 'X-Requested-With': 'fetch', Accept: 'application/json' },
    })
      .then(function (resp) {
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        return resp.json();
      })
      .then(function (data) {
        if (!data.ok) throw new Error(data.error || 'fetch failed');
        if (!data.rows || !data.rows.length) {
          finish();
          return;
        }
        data.rows.forEach(function (row) {
          loaded += 1;
          tbody.appendChild(buildRow(row, loaded));
        });
        if (countEl) countEl.textContent = String(loaded);
        document.dispatchEvent(new Event('lm-rows-added'));
        if (typeof data.total === 'number' && data.total > 0) {
          total = data.total;
          table.setAttribute('data-total', String(total));
        }
        if (total > 0 && loaded >= total) {
          finish();
          return;
        }
        if (data.has_more !== true && data.rows.length < pageSize) {
          finish();
          return;
        }
        if (needsFill()) {
          window.setTimeout(loadMore, 20);
        }
      })
      .catch(function (err) {
        setLoading(true, 'تعذّر التحميل — اسحب للأسفل للمحاولة');
        finished = false;
        console.warn('low-margin load failed', err);
      })
      .finally(function () {
        if (!loader || loader.textContent.indexOf('تعذّر') !== 0) {
          setLoading(false);
        }
      });
  }

  function onScroll() {
    if (!canLoadMore()) return;
    if (nearBottom()) loadMore();
  }

  wrap.addEventListener('scroll', onScroll, { passive: true });

  if (!finished) {
    if (needsFill() || loaded < pageSize) {
      loadMore();
    }
  }
})();
