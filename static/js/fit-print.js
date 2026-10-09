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
      '@media print { @page { size: A4 landscape; margin: 8mm 8mm 14mm 8mm; ' +
      '@bottom-left { content: "صفحة " counter(page) " من " counter(pages); font: 9px Tahoma, sans-serif; color: #6B7785; } } }';
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

  /* زر داخل لوحة بلا جدول (مخطط، قائمة…): اللوحة نفسها هي المطلوب طباعتها لا الصفحة كلها */
  function panelWithoutTables(btn) {
    var panel = btn.closest('.dash-panel');
    if (panel && !collectTables(panel).length) return panel;
    return null;
  }

  function markPanel(panel) {
    panel.classList.add('is-print-target');
    var el = panel.parentElement;
    while (el && el !== document.body) {
      el.classList.add('is-print-ancestor');
      el = el.parentElement;
    }
    document.body.classList.add('fit-table-only');
    document.body.setAttribute('data-print-scope', 'panel');
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
        /* التصغير يُطبَّق من CSS داخل @media print فقط (zoom: var(--fit-print-scale)) */
        table.style.setProperty('--fit-print-scale', String(scale));
      }
    });
    document.body.classList.remove('fit-measuring');
  }


  /* ترويسة وتذييل التقرير الرسمي (تظهر في الطباعة فقط) */
  function txt(el) { return el ? (el.textContent || '').replace(/\s+/g, ' ').trim() : ''; }

  function filterSummary() {
    var out = [];
    var main = document.querySelector('main') || document.body;
    main.querySelectorAll('form select, form input[type="text"], form input[type="date"], form input[type="month"], form input[type="search"]').forEach(function (f) {
      if (f.closest('[hidden]') || f.type === 'hidden') return;
      var val = f.tagName === 'SELECT' ? txt(f.options[f.selectedIndex]) : (f.value || '').trim();
      if (!val || val.length > 28) return;
      var lab = f.id ? main.querySelector('label[for="' + f.id + '"]') : null;
      if (!lab) {
        var wrap = f.closest('.fl-field, .field');
        lab = wrap ? wrap.querySelector('label') : null;
      }
      var name = txt(lab);
      if (!name) return;
      out.push(name.replace(/[:：]\s*$/, '') + ': ' + val);
    });
    return out.slice(0, 8);
  }

  function removeReportFrame() {
    document.querySelectorAll('.print-report-head, .print-report-foot').forEach(function (el) { el.remove(); });
  }

  function addReportFrame(tables) {
    removeReportFrame();
    var main = document.querySelector('main') || document.body;
    var pageTitle = txt(main.querySelector('.dash-head h2, .dash-head h1, h1, h2')) || document.title;
    var first = tables && tables[0];
    var panel = first && first.closest('.dash-panel, .up-sheet, .lm-sheet, .vt-sheet, .wqc-sheet, section');
    var sub = txt(panel && panel.querySelector('.dash-panel-head h3, .up-sheet-head h2, .up-sheet-head h3, h3, h2'));
    if (sub === pageTitle) sub = '';
    var rows = 0;
    (tables || []).forEach(function (t) { rows += t.querySelectorAll('tbody tr').length; });
    var user = txt(document.querySelector('.sidebar-user-name'));
    var now = new Date();
    var pad = function (n) { return (n < 10 ? '0' : '') + n; };
    var stamp = now.getFullYear() + '/' + pad(now.getMonth() + 1) + '/' + pad(now.getDate()) + '  ' + pad(now.getHours()) + ':' + pad(now.getMinutes());
    (tables || []).forEach(function (t) {
      var cols = t.querySelectorAll('thead tr:last-child th').length;
      t.setAttribute('data-prt-cols', String(cols));
    });
    var info = ['عدد السجلات: ' + rows].concat(filterSummary());
    var esc = function (v) { return String(v).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); };
    var head = document.createElement('div');
    head.className = 'print-report-head';
    head.innerHTML =
      '<div class="prh-top"><div><h1 class="prh-title">' + esc(sub || pageTitle) + '</h1>' +
      (sub ? '<p class="prh-sub">' + esc(pageTitle) + '</p>' : '') + '</div>' +
      '<div class="prh-org"><b>منصة التحليل</b><span>تاريخ الإصدار: ' + stamp + '</span>' +
      (user ? '<span>أصدره: ' + esc(user) + '</span>' : '') + '</div></div>' +
      '';
    main.insertBefore(head, main.firstChild);
    var foot = document.createElement('div');
    foot.className = 'print-report-foot';
    foot.textContent = 'تقرير صادر من منصة التحليل — للاستخدام الداخلي';
    document.body.appendChild(foot);
  }

  function prepare(tables, panel) {
    ensureLandscapeStyle();
    clearPrintTargets();
    clearTableFit();
    document.body.classList.add('fit-printing');
    document.body.classList.add('print-landscape');
    if (panel) {
      markPanel(panel);
    } else {
      if (!tables || !tables.length) {
        tables = collectTables(document.querySelector('main') || document.body);
      }
      markTables(tables);
    }
    addReportFrame(panel ? collectTables(panel) : tables);
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

  /* ── ثيم الطباعة: خلفية بيضاء + ألوان نص/حدود مناسبة لها ─────────────────
     الشاشات تحدّد ألوانها بـ !important (نيون على كحلي). عند التصدير نحسب
     اللون الفعلي لكل عنصر ونحوّله مع الحفاظ على الدرجة اللونية، ثم نعيد
     القيم الأصلية بعد الطباعة. */
  var themed = [];
  var themedOn = false;

  function parseColor(str) {
    if (!str) return null;
    var m = str.match(/rgba?\(\s*([\d.]+)[ ,]+([\d.]+)[ ,]+([\d.]+)(?:[ ,/]+([\d.]+%?))?\s*\)/);
    if (m) {
      var a = m[4] === undefined ? 1 : (m[4].indexOf('%') > -1 ? parseFloat(m[4]) / 100 : parseFloat(m[4]));
      return { r: +m[1], g: +m[2], b: +m[3], a: a };
    }
    m = str.match(/color\(srgb\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)(?:\s*\/\s*([\d.]+%?))?\)/);
    if (m) {
      var a2 = m[4] === undefined ? 1 : (m[4].indexOf('%') > -1 ? parseFloat(m[4]) / 100 : parseFloat(m[4]));
      return { r: m[1] * 255, g: m[2] * 255, b: m[3] * 255, a: a2 };
    }
    return null;
  }

  function lum(c) {
    return (0.2126 * c.r + 0.7152 * c.g + 0.0722 * c.b) / 255;
  }

  function toHsl(c) {
    var r = c.r / 255, g = c.g / 255, b = c.b / 255;
    var max = Math.max(r, g, b), min = Math.min(r, g, b);
    var h = 0, s = 0, l = (max + min) / 2;
    if (max !== min) {
      var d = max - min;
      s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
      if (max === r) h = (g - b) / d + (g < b ? 6 : 0);
      else if (max === g) h = (b - r) / d + 2;
      else h = (r - g) / d + 4;
      h *= 60;
    }
    return { h: h, s: s, l: l };
  }

  function hsl(h, s, l) {
    return 'hsl(' + Math.round(h) + ',' + Math.round(s * 100) + '%,' + Math.round(l * 100) + '%)';
  }

  /* نص فاتح (للخلفية الداكنة) → نفس اللون بدرجة غامقة مقروءة على الأبيض */
  function printText(c) {
    if (lum(c) < 0.42) return null;
    var p = toHsl(c);
    if (p.s < 0.2 || isNavyHue(p)) return '#0f172a';
    return hsl(p.h, Math.min(p.s, 0.9), 0.26);
  }

  /* الأزرق/الكحلي هو لون الثيم نفسه لا لون دلالي: يتحوّل إلى أبيض/نص داكن محايد */
  function isNavyHue(p) {
    return p.h >= 200 && p.h <= 260 && p.s < 0.75;
  }

  /* خلفية داكنة → درجة فاتحة جدًا من نفس اللون؛ الشفافة تُلغى */
  function printBg(c) {
    if (c.a < 0.5) return 'transparent';
    if (lum(c) > 0.62) return null;
    if (lum(c) > 0.3) return null;
    var p = toHsl(c);
    return p.s < 0.2 || isNavyHue(p) ? '#ffffff' : hsl(p.h, Math.min(p.s, 0.6), 0.94);
  }

  function printBorder(c) {
    if (c.a < 0.05) return null;
    if (lum(c) < 0.55 && c.a >= 0.5) return null;
    var p = toHsl(c);
    return p.s < 0.2 ? '#cbd5e1' : hsl(p.h, Math.min(p.s, 0.5), 0.72);
  }

  /* القيم لا تُكتب على العنصر بل في ورقة @media print فقط، فالشاشة لا تتأثر أبدًا.
     العناصر التي لها نفس الإعلانات تتشارك كلاسًا واحدًا (pt-N). */
  var ptRules = {};
  var ptCount = 0;
  var ptStyle = null;

  function setImp(el, prop, val, store) {
    store.push(prop + ':' + val + ' !important');
  }

  function themeElement(el) {
    if (el.closest && el.closest('.print-report-head, .print-report-foot, table')) return;
    var cs = window.getComputedStyle(el);
    if (cs.display === 'none') return;
    var store = [];
    var col = parseColor(cs.color);
    if (col) {
      var t = printText(col);
      if (t) setImp(el, 'color', t, store);
    }
    var bgc = parseColor(cs.backgroundColor);
    var hasImg = cs.backgroundImage && cs.backgroundImage !== 'none' && cs.backgroundImage.indexOf('url(') === -1;
    if (hasImg) setImp(el, 'background-image', 'none', store);
    if (bgc && bgc.a > 0) {
      var b = printBg(bgc);
      if (b) setImp(el, 'background-color', b, store);
    }
    ['top', 'right', 'bottom', 'left'].forEach(function (side) {
      if (parseFloat(cs['border' + side.charAt(0).toUpperCase() + side.slice(1) + 'Width']) > 0) {
        var bc = parseColor(cs['border' + side.charAt(0).toUpperCase() + side.slice(1) + 'Color']);
        if (bc) {
          var v = printBorder(bc);
          if (v) setImp(el, 'border-' + side + '-color', v, store);
        }
      }
    });
    if (cs.boxShadow && cs.boxShadow !== 'none') setImp(el, 'box-shadow', 'none', store);
    if (cs.textShadow && cs.textShadow !== 'none') setImp(el, 'text-shadow', 'none', store);
    if (el instanceof SVGElement) {
      var fill = parseColor(cs.fill);
      if (fill) {
        var f = printText(fill);
        if (f) setImp(el, 'fill', f, store);
      }
    }
    if (!store.length) return;
    var decl = store.join(';');
    var cls = ptRules[decl];
    if (!cls) {
      cls = ptRules[decl] = 'pt-' + (++ptCount);
    }
    el.classList.add(cls);
    themed.push(el);
  }

  function applyPrintTheme() {
    /* الواجهة كلها فاتحة الآن: لا تحويل لألوان الطباعة حتى تظهر الألوان الحقيقية كما على الشاشة */
    return;
    if (themedOn || !document.body.classList.contains('theme-navy')) return;
    themedOn = true;
    var root = document.querySelector('main') || document.body;
    [document.documentElement, document.body, root.parentElement, root].forEach(function (el) {
      if (el) themeElement(el);
    });
    root.querySelectorAll('*').forEach(themeElement);

    /* خصوصية عالية حتى تتفوّق على أقفال ثيم Navy (!important متعددة الكلاسات) */
    var css = '@media print{';
    Object.keys(ptRules).forEach(function (decl) {
      var sel = ('.' + ptRules[decl]).repeat(16);
      /* الوصفاء + body + html نفسهما (لا يطابقهما محدد «html body .x») */
      /* :not(#id) يضيف خصوصية معرّف حتى تتفوّق على أقفال الثيم المربوطة بـ #id */
      var tail = ':not(#fit-pt-a):not(#fit-pt-b):not(#fit-pt-c)';
      css += 'html body ' + sel + tail + ',html body' + sel + tail + ',html' + sel + tail + '{' + decl + '}';
    });
    css += '}';
    ptStyle = document.createElement('style');
    ptStyle.id = 'fit-print-theme';
    ptStyle.textContent = css;
    document.head.appendChild(ptStyle);
  }

  function restorePrintTheme() {
    themed.forEach(function (el) {
      var drop = [];
      el.classList.forEach(function (c) {
        if (/^pt-\d+$/.test(c)) drop.push(c);
      });
      drop.forEach(function (c) { el.classList.remove(c); });
      if (!el.getAttribute('class')) el.removeAttribute('class');
    });
    if (ptStyle && ptStyle.parentNode) ptStyle.parentNode.removeChild(ptStyle);
    ptStyle = null;
    ptRules = {};
    ptCount = 0;
    themed = [];
    themedOn = false;
  }

  function cleanup() {
    removeReportFrame();
    restorePrintTheme();
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
    var panel = panelWithoutTables(btn);
    prepare(panel ? [] : tablesForButton(btn), panel);
    window.setTimeout(function () {
      fitVisibleTables();
      applyPrintTheme();
      window.print();
      /* print() يعود بعد إغلاق نافذة المعاينة؛ نعيد الشاشة لحالتها حتى لو لم يصل afterprint */
      window.setTimeout(cleanup, 300);
    }, 40);
  }

  function boot() {
    ensurePanelPdfButtons();
    /* لوحات تُحمَّل جداولها لاحقًا (بعد «عرض» أو من API): نضيف لها زر PDF بالقالب نفسه */
    if (window.MutationObserver) {
      var timer = null;
      new MutationObserver(function () {
        if (timer) return;
        timer = window.setTimeout(function () { timer = null; ensurePanelPdfButtons(); }, 400);
      }).observe(document.querySelector('main') || document.body, { childList: true, subtree: true });
    }
  }

  document.addEventListener('click', onClick);
  window.addEventListener('beforeprint', function () {
    if (!document.body.classList.contains('fit-printing')) {
      prepare(collectTables(document.querySelector('main') || document.body));
    } else {
      ensureLandscapeStyle();
    }
    applyPrintTheme();
  });
  window.addEventListener('afterprint', cleanup);
  if (window.matchMedia) {
    var printMql = window.matchMedia('print');
    var onPrintChange = function (e) {
      if (!e.matches) cleanup();
    };
    if (printMql.addEventListener) printMql.addEventListener('change', onPrintChange);
    else if (printMql.addListener) printMql.addListener(onPrintChange);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
