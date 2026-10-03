/**
 * تلوين أعمدة الجداول في كل الصفحات، بلا تكرار للون داخل الجدول الواحد:
 *   tc-name   عمود الاسم (أول عمود نصي بعد «#»)           ذهبي
 *   tcc-1…9   كل عمود رقمي/قيمي يأخذ لوناً خفيفاً مختلفاً   (يُحدَّد من نوع العمود، وإن استُهلك اللون يؤخذ أول لون حرّ)
 *   tc-num    خلية رقمية (توسيط)   ·   tc-head-num  عنوانها
 * ضبط يدوي عند الحاجة (على وسم <table>):
 *   data-no-colors      لتعطيل التلوين لهذا الجدول
 *   data-name-col="N"   لتحديد عمود الاسم يدوياً (يبدأ العدّ من 0)
 * نوع العمود يُستنتج من نص رأس العمود. يدعم الرؤوس ذات الصفين (colspan/rowspan)،
 * وصفوف tfoot، وجداول الإجمالي المنفصلة التي تلي الجدول (…-foot)،
 * وأعمدة المخازن في أوراق المقارنة.
 */
(function () {
  "use strict";

  var SKIP_TABLE = /matrix|editor|pivot/i;
  var NUMERIC = /^[\s\d.,:%+\-\/٠-٩٫٬ر.س]*$/;
  var PALETTE = 9;
  // لون مفضّل لكل نوع حتى يبقى المعنى ثابتاً بين الجداول ما أمكن
  var RULES = [
    ["avg", 4, /متوسط/],
    ["share", 5, /الحصة|حصة|نسبة|%|هامش|الدوران/],
    ["count", 1, /فواتير|عدد|أصناف|الأصناف|طلبات|الطلبات|الفروع|المخازن|مخازن|المستندات|الأسطر|مرات|الحركات|الموردين|مطلوب/],
    ["ret", 3, /مرتجع|إرجاع|ارجاع/],
    [
      "amt",
      2,
      /المبيعات|المشتريات|القيمة|قيمة|المبلغ|التكلفة|تكلفة|الإجمالي|الاجمالي|إجمالي|اجمالي|السعر|سعر|الربح|الصافي|مدين|دائن|الخصم|الضريبة|المصروف|المستحق|السداد/,
    ],
    ["qty", 6, /الكمية|كمية|الرصيد|رصيد|المتبقي|وارد|مباع|متبقي/],
  ];
  var CLEAN = ["tc-name", "tc-num", "tc-head-num"];
  for (var i0 = 1; i0 <= PALETTE; i0++) CLEAN.push("tcc-" + i0);
  // فئات الإصدار السابق من السكربت
  var LEGACY = ["tc-count", "tc-amt", "tc-ret", "tc-avg", "tc-share", "tc-qty"];

  function preferredOf(text) {
    var t = String(text || "").replace(/\s+/g, " ").trim();
    for (var i = 0; i < RULES.length; i++) {
      if (RULES[i][2].test(t)) return RULES[i][1];
    }
    return 0;
  }

  function isNumericText(text) {
    var t = String(text || "").trim();
    return !t || t === "—" || NUMERIC.test(t);
  }

  /** رؤوس الأعمدة المنطقية (تفكّ colspan/rowspan وتأخذ الخلية الورقية الأدنى). */
  function headerModel(table) {
    var thead = table.tHead;
    if (!thead || !thead.rows.length || thead.rows.length > 3) return null;
    var grid = [];
    var rows = thead.rows;
    var ncols = 0;
    for (var r = 0; r < rows.length; r++) {
      grid[r] = grid[r] || [];
      var col = 0;
      for (var k = 0; k < rows[r].cells.length; k++) {
        var cell = rows[r].cells[k];
        while (grid[r][col]) col++;
        for (var dr = 0; dr < cell.rowSpan; dr++) {
          grid[r + dr] = grid[r + dr] || [];
          for (var dc = 0; dc < cell.colSpan; dc++) grid[r + dr][col + dc] = cell;
        }
        col += cell.colSpan;
        ncols = Math.max(ncols, col);
      }
    }
    var heads = [];
    var groups = [];
    for (var c = 0; c < ncols; c++) {
      var leaf = grid[rows.length - 1] && grid[rows.length - 1][c];
      if (!leaf) return null;
      heads[c] = leaf;
      groups[c] = rows.length > 1 && grid[0][c] !== leaf ? grid[0][c] : null;
    }
    return { heads: heads, groups: groups, ncols: ncols };
  }

  /** خلية الصف عند عمود منطقي: [{cell, start}] لكل خلية بدايةً من العمود المنطقي. */
  function rowCells(row) {
    var out = [];
    var col = 0;
    for (var i = 0; i < row.cells.length; i++) {
      out.push({ cell: row.cells[i], start: col });
      col += row.cells[i].colSpan;
    }
    return { cells: out, total: col };
  }

  function dataRows(table, ncols, limit) {
    var rows = [];
    for (var b = 0; b < table.tBodies.length; b++) {
      for (var r = 0; r < table.tBodies[b].rows.length; r++) {
        var rc = rowCells(table.tBodies[b].rows[r]);
        if (rc.total === ncols) rows.push(rc);
        if (limit && rows.length >= limit) return rows;
      }
    }
    return rows;
  }

  function cellAt(rc, col) {
    for (var i = 0; i < rc.cells.length; i++) {
      if (rc.cells[i].start === col) return rc.cells[i].cell;
    }
    return null;
  }

  function applyClasses(cell, want) {
    for (var l = 0; l < LEGACY.length; l++) cell.classList.remove(LEGACY[l]);
    for (var j = 0; j < CLEAN.length; j++) {
      var has = cell.classList.contains(CLEAN[j]);
      var keep = want.indexOf(CLEAN[j]) >= 0;
      if (has && !keep) cell.classList.remove(CLEAN[j]);
      if (!has && keep) cell.classList.add(CLEAN[j]);
    }
  }

  function wantFor(col, nameCol, colorOf) {
    if (col === nameCol) return ["tc-name"];
    if (colorOf[col]) return ["tcc-" + colorOf[col], "tc-num"];
    return [];
  }

  function paintRows(rows, nameCol, colorOf) {
    rows.forEach(function (rc) {
      rc.cells.forEach(function (c) {
        if (c.cell.colSpan > 1) {
          applyClasses(c.cell, []);
          return;
        }
        applyClasses(c.cell, wantFor(c.start, nameCol, colorOf));
      });
    });
  }

  /** جداول الإجمالي المنفصلة التي تلي الجدول مباشرةً (…-foot). */
  function companionFooters(table) {
    var anchor = table.closest(".table-wrap, [class*='-scroll'], [class*='-wrap']") || table;
    var out = [];
    var sib = anchor.nextElementSibling;
    while (sib && out.length < 2) {
      if (sib.tagName === "TABLE" && /foot/i.test(sib.className || "")) out.push(sib);
      else if (sib.querySelector && sib.tagName !== "TABLE") {
        var inner = sib.querySelector("table[class*='foot']");
        if (inner) out.push(inner);
      }
      sib = sib.nextElementSibling;
    }
    return out;
  }

  function classify(table) {
    if (!table.tBodies.length) return;
    if (table.hasAttribute("data-no-colors")) return;
    var cls = String(table.className || "");
    if (SKIP_TABLE.test(cls) || SKIP_TABLE.test(table.id || "")) return;
    var model = headerModel(table);
    if (!model || model.ncols < 3) return;
    var heads = model.heads;
    var ncols = model.ncols;
    var sample = dataRows(table, ncols, 40);
    if (!sample.length) return;
    if (table.querySelector("tbody input, tbody select, tbody textarea")) return;

    var pref = [];
    var isWh = [];
    var numericCols = 0;
    var c;
    for (c = 0; c < ncols; c++) {
      var label = heads[c].textContent;
      if (model.groups[c]) label = model.groups[c].textContent + " " + label;
      pref[c] = preferredOf(heads[c].textContent) || preferredOf(label);
      isWh[c] = /sheet-wh/.test(String(heads[c].className || ""));
      if (pref[c] || isWh[c]) numericCols++;
    }

    // عمود الاسم: أول عمود نصي ليس رقمياً (يُتجاوز عمود الرقم/الكود)
    var nameCol = -1;
    for (var n = 0; n < ncols; n++) {
      var ht = heads[n].textContent.replace(/\s+/g, "").trim();
      if (ht === "#" || ht === "م" || ht === "") continue;
      if (pref[n] || isWh[n]) break;
      var textual = 0;
      sample.forEach(function (rc) {
        var cell = cellAt(rc, n);
        if (cell && !isNumericText(cell.textContent)) textual++;
      });
      if (textual >= Math.ceil(sample.length / 2)) {
        nameCol = n;
        break;
      }
    }

    var forced = parseInt(table.getAttribute("data-name-col"), 10);
    if (!isNaN(forced) && forced >= 0 && forced < ncols) nameCol = forced;
    if (!numericCols && nameCol < 0) return;

    // توزيع الألوان: أول عمود من كل نوع يأخذ لونه المفضّل، والمكرَّر يأخذ أول لون غير مستخدم
    var used = {};
    var usedCount = 0;
    var colorOf = [];
    for (c = 0; c < ncols; c++) {
      if (!pref[c] || c === nameCol || used[pref[c]]) continue;
      used[pref[c]] = true;
      usedCount++;
      colorOf[c] = pref[c];
    }
    for (c = 0; c < ncols; c++) {
      if ((!pref[c] && !isWh[c]) || c === nameCol || colorOf[c]) continue;
      var want = 0;
      for (var p = 1; p <= PALETTE; p++) {
        if (!used[p]) {
          want = p;
          break;
        }
      }
      if (want) {
        used[want] = true;
      } else {
        want = (usedCount % PALETTE) + 1;
      }
      usedCount++;
      colorOf[c] = want;
    }

    paintRows(dataRows(table, ncols, 0), nameCol, colorOf);
    if (table.tFoot) {
      var foot = [];
      for (var f = 0; f < table.tFoot.rows.length; f++) {
        var frc = rowCells(table.tFoot.rows[f]);
        if (frc.total === ncols) foot.push(frc);
      }
      paintRows(foot, nameCol, colorOf);
    }
    companionFooters(table).forEach(function (ft) {
      var frows = [];
      for (var b = 0; b < ft.tBodies.length; b++) {
        for (var r = 0; r < ft.tBodies[b].rows.length; r++) {
          var rc = rowCells(ft.tBodies[b].rows[r]);
          if (rc.total === ncols) frows.push(rc);
        }
      }
      paintRows(frows, -1, colorOf);
    });
    for (var h = 0; h < ncols; h++) {
      if (colorOf[h]) heads[h].classList.add("tc-head-num");
    }
  }

  function run(root) {
    (root || document).querySelectorAll("main table").forEach(classify);
  }

  var timer = null;
  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(function () {
      run(document);
    }, 120);
  }

  function init() {
    run(document);
    var main = document.querySelector("main") || document.body;
    if (window.MutationObserver) {
      new MutationObserver(schedule).observe(main, { childList: true, subtree: true });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
