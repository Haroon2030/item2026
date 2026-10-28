/* تصدير جداول تحليل المبيعات PDF: الصفحة الأولى نقاط البيع ونظام المبيعات، والثانية المجموعات.
   محتوى الجداول فقط (مع الإجماليات) في مستند طباعة مستقل داخل iframe مخفي، فلا تتأثر الشاشة. */
(function () {
  'use strict';

  function txt(el) { return el ? (el.textContent || '').replace(/\s+/g, ' ').trim() : ''; }
  function esc(v) { return String(v).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }

  function panelTable(panel) {
    if (!panel) return null;
    var table = panel.querySelector('table');
    if (!table) return null;
    return { title: txt(panel.querySelector('.dash-panel-head h3')), table: table };
  }

  /* نسخة نظيفة من الجدول: بلا معرّفات ولا تلميحات ولا صفوف التحميل، والإجمالي ظاهر */
  function cleanClone(table) {
    var t = table.cloneNode(true);
    t.removeAttribute('id');
    t.className = 'xt';
    t.querySelectorAll('[id]').forEach(function (el) { el.removeAttribute('id'); });
    t.querySelectorAll('[title]').forEach(function (el) { el.removeAttribute('title'); });
    t.querySelectorAll('tfoot[hidden]').forEach(function (el) { el.remove(); });
    t.querySelectorAll('.sales-ov-loading').forEach(function (el) { el.closest('tr').remove(); });
    return t.outerHTML;
  }

  function filters() {
    var out = [];
    var from = document.getElementById('sales-from');
    var to = document.getElementById('sales-to');
    if (from && to && from.value && to.value) out.push('الفترة: من ' + from.value + ' إلى ' + to.value);
    ['sales-branch', 'sales-group'].forEach(function (id) {
      var sel = document.getElementById(id);
      if (sel && sel.selectedIndex >= 0) {
        var label = id === 'sales-branch' ? 'الفرع' : 'المجموعة';
        out.push(label + ': ' + txt(sel.options[sel.selectedIndex]));
      }
    });
    return out;
  }

  function stamp() {
    var d = new Date();
    var p = function (n) { return (n < 10 ? '0' : '') + n; };
    return d.getFullYear() + '/' + p(d.getMonth() + 1) + '/' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
  }

  var CSS =
    '@page { size: A4 portrait; margin: 12mm 10mm 14mm 10mm;' +
    ' @bottom-left { content: "صفحة " counter(page) " من " counter(pages); font: 9px Tahoma, Arial, sans-serif; color: #6b7785; } }' +
    '* { box-sizing: border-box; -webkit-print-color-adjust: exact; print-color-adjust: exact; }' +
    'html, body { margin: 0; background: #fff; color: #1f2a37; font-family: Tahoma, Arial, sans-serif; }' +
    '.page { break-after: page; }' +
    '.page:last-child { break-after: auto; }' +
    '.head { display: flex; justify-content: space-between; align-items: flex-end; gap: 12px;' +
    ' border-bottom: 2px solid #1f3b63; padding-bottom: 6px; margin-bottom: 10px; }' +
    '.head h1 { margin: 0; font-size: 17px; color: #1f3b63; }' +
    '.head p { margin: 3px 0 0; font-size: 10.5px; color: #5a6675; }' +
    '.head .meta { font-size: 9.5px; color: #6b7785; text-align: left; line-height: 1.5; }' +
    'h2 { margin: 14px 0 6px; font-size: 13px; color: #1f2a37; }' +
    'h2:first-of-type { margin-top: 4px; }' +
    'table.xt { width: 100%; border-collapse: collapse; font-size: 10.5px; }' +
    'table.xt thead { display: table-header-group; }' +
    'table.xt tfoot { display: table-row-group; }' +
    'table.xt tr { break-inside: avoid; }' +
    'table.xt th { background: #1f3b63; color: #fff; font-weight: 700; padding: 5px 6px; border: 1px solid #1f3b63; text-align: center; }' +
    'table.xt td { padding: 4px 6px; border: 1px solid #d5dbe3; text-align: center; }' +
    'table.xt td:nth-child(2) { text-align: right; }' +
    'table.xt tbody tr:nth-child(even) td { background: #f5f7fa; }' +
    'table.xt tr.sales-zero-row td { color: #8a94a1; }' +
    'table.xt tfoot td { background: #e3eaf3; font-weight: 700; border-top: 2px solid #1f3b63; }' +
    'table.xt td[colspan] { text-align: right; }' +
    'table.xt .mono { font-variant-numeric: tabular-nums; white-space: nowrap; }' +
    '.money { display: inline-flex; align-items: center; gap: 2px; direction: ltr; }' +
    '.money svg { width: 0.85em; height: 0.85em; fill: currentColor; }' +
    '.note { margin-top: 8px; font-size: 9.5px; color: #6b7785; }';

  function page(title, sub, sections) {
    var body = sections.map(function (s) {
      /* جدول وحيد بعنوان الصفحة نفسه: لا يُكرَّر العنوان */
      return (s.title && s.title !== title ? '<h2>' + esc(s.title) + '</h2>' : '') + cleanClone(s.table);
    }).join('');
    return '<section class="page"><div class="head"><div><h1>' + esc(title) + '</h1><p>' + esc(sub) + '</p></div>' +
      '<div class="meta">تحليل المبيعات<br>تاريخ الإصدار: ' + stamp() + '</div></div>' + body +
      '<p class="note">كل المبالغ بدون ضريبة القيمة المضافة.</p></section>';
  }

  function build() {
    var slot = document.querySelector('[data-gm-slot="tables"]');
    var branchPanels = slot ? Array.prototype.map.call(slot.querySelectorAll('.dash-panel'), panelTable).filter(Boolean) : [];
    var groups = panelTable(document.getElementById('sales-groups-panel'));
    if (!branchPanels.length && !groups) return null;
    if (groups && groups.table.querySelector('.sales-ov-loading')) {
      window.alert('جدول المجموعات لم يكتمل تحميله بعد — انتظر قليلاً ثم أعد التصدير.');
      return null;
    }
    var sub = filters().join(' · ');
    var pages = [];
    if (branchPanels.length) pages.push(page('مبيعات الفروع', sub, branchPanels));
    if (groups) pages.push(page(groups.title || 'مبيعات المجموعات', sub, [groups]));
    var from = document.getElementById('sales-from');
    var to = document.getElementById('sales-to');
    var name = 'مبيعات الفروع والمجموعات' + (from && to ? ' ' + from.value + '_' + to.value : '');
    return '<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8"><title>' + esc(name) + '</title>' +
      '<style>' + CSS + '</style></head><body>' + pages.join('') + '</body></html>';
  }

  function exportTables(ev) {
    ev.preventDefault();
    ev.stopPropagation(); /* لا يلتقطه fit-print.js (يستمع لكل .dash-pdf-btn على مستوى المستند) */
    var html = build();
    if (!html) return;
    var old = document.getElementById('sales-tables-print');
    if (old) old.remove();
    var frame = document.createElement('iframe');
    frame.id = 'sales-tables-print';
    frame.setAttribute('aria-hidden', 'true');
    frame.style.cssText = 'position:fixed;width:0;height:0;border:0;left:-9999px;top:0;';
    document.body.appendChild(frame);
    var doc = frame.contentDocument;
    doc.open();
    doc.write(html);
    doc.close();
    /* عنوان الصفحة الأم يصبح اسم ملف PDF المقترح في بعض المتصفحات */
    var oldTitle = document.title;
    document.title = doc.title;
    window.setTimeout(function () {
      frame.contentWindow.focus();
      frame.contentWindow.print();
      window.setTimeout(function () { document.title = oldTitle; }, 500);
    }, 60);
  }

  function boot() {
    document.querySelectorAll('.js-sales-tables-pdf').forEach(function (btn) {
      btn.addEventListener('click', exportTables);
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
