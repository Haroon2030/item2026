(function () {
  "use strict";

  // الفروع الأكثر مرتجعاً: شريط لكل فرع، طوله نسبة المرتجع من المبيعات (أو مبلغه) مع خط للمتوسط العام.
  // الفروع الأعلى من المتوسط بلون عنبري، والفرع بمرتجعات قليلة يُنبَّه أن عيّنته صغيرة (3 مرتجعات لا تعني خللاً).
  var SMALL_SAMPLE = 10;
  var mode = "amount"; // amount | rate — الافتراضي المبلغ لأن الريال هو ما يهم قرار المتابعة
  var lastRows = [];

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function money(n) {
    return Number(n || 0).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function rateOf(r) {
    var gross = Number(r.gross_total) || 0;
    return gross > 0 ? (Math.abs(Number(r.amount) || 0) / gross) * 100 : 0;
  }

  function renderReturns(rows) {
    var board = document.getElementById("sales-returns-board");
    var empty = document.getElementById("sales-returns-empty");
    lastRows = rows || [];
    var list = lastRows.slice();
    if (!board) return;

    if (!list.length) {
      board.hidden = true;
      board.innerHTML = "";
      if (empty) empty.hidden = false;
      return;
    }
    if (empty) empty.hidden = true;
    board.hidden = false;

    var totalAmount = 0, totalGross = 0, totalInv = 0;
    list.forEach(function (r) {
      totalAmount += Math.abs(Number(r.amount) || 0);
      totalGross += Number(r.gross_total) || 0;
      totalInv += Number(r.invoice_count) || 0;
    });
    var overallRate = totalGross > 0 ? (totalAmount / totalGross) * 100 : 0;

    list.sort(function (a, b) {
      return mode === "rate"
        ? rateOf(b) - rateOf(a)
        : Math.abs(Number(b.amount) || 0) - Math.abs(Number(a.amount) || 0);
    });

    var maxVal = 0;
    list.forEach(function (r) {
      var v = mode === "rate" ? rateOf(r) : Math.abs(Number(r.amount) || 0);
      if (v > maxVal) maxVal = v;
    });
    // خط المتوسط يُرسم في وضع النسبة فقط (في وضع المبلغ لا معنى لمتوسط الريال بين فروع مختلفة الحجم)
    var avgPos = mode === "rate" && maxVal > 0 ? Math.min(100, (overallRate / maxVal) * 100) : null;

    var html =
      '<div class="rv-top">' +
      '<p class="rv-insight"><b class="mono">' + esc(money(totalAmount)) + "</b> ر.س مرتجع، أي <b class=\"mono\">" +
      esc(overallRate.toFixed(2)) + "%</b> من المبيعات · " + esc(String(totalInv)) + " مرتجع في " + esc(String(list.length)) + " فروع</p>" +
      '<div class="rv-seg" role="tablist" aria-label="ترتيب الفروع">' +
      '<button type="button" role="tab" data-mode="amount" aria-selected="' + (mode === "amount") + '">حسب المبلغ</button>' +
      '<button type="button" role="tab" data-mode="rate" aria-selected="' + (mode === "rate") + '">حسب النسبة</button>' +
      "</div></div>" +
      '<ul class="rv-list" role="list">';

    list.forEach(function (row) {
      var rate = rateOf(row);
      var amount = Math.abs(Number(row.amount) || 0);
      var v = mode === "rate" ? rate : amount;
      var width = maxVal > 0 ? Math.max(1.5, (v / maxVal) * 100) : 0;
      var high = rate > overallRate;
      var small = (Number(row.invoice_count) || 0) < SMALL_SAMPLE;
      var primary = mode === "rate" ? rate.toFixed(2) + "%" : money(amount);
      var secondary =
        mode === "rate"
          ? money(amount) + " · " + (row.invoice_count || 0) + " مرتجع"
          : rate.toFixed(2) + "% من " + (row.gross_total_display || "") + " · " + (row.invoice_count || 0) + " مرتجع";
      html +=
        '<li class="rv-row' + (high ? " is-high" : "") + (small ? " is-small" : "") + '"' +
        ' title="' + esc(row.name) + " — مرتجع " + esc(money(amount)) + " (" + rate.toFixed(2) + "% من مبيعات " +
        esc(row.gross_total_display || "") + ")" + (small ? " — عيّنة صغيرة" : "") + '">' +
        '<span class="rv-name">' + esc(row.name) +
        (small ? '<em class="rv-tag">عيّنة صغيرة</em>' : "") + "</span>" +
        '<span class="rv-track" aria-hidden="true"><span class="rv-fill" style="width:' + width.toFixed(1) + '%"></span>' +
        (avgPos == null ? "" : '<i class="rv-avg" style="inset-inline-start:' + avgPos.toFixed(1) + '%"></i>') + "</span>" +
        '<span class="rv-val"><b class="mono">' + esc(primary) + '</b><small class="mono">' + esc(secondary) + "</small></span></li>";
    });
    html += "</ul>";
    html +=
      '<p class="rv-legend"><span class="rv-key is-high"></span> أعلى من متوسط الشركة (' + esc(overallRate.toFixed(2)) + '%) ' +
      '<span class="rv-key"></span> أقل من المتوسط' +
      (avgPos == null ? "" : ' <span class="rv-key rv-key-line"></span> خط المتوسط') + "</p>";
    board.innerHTML = html;

    var btns = board.querySelectorAll(".rv-seg button");
    for (var i = 0; i < btns.length; i++) {
      btns[i].addEventListener("click", function (ev) {
        mode = ev.currentTarget.getAttribute("data-mode") === "rate" ? "rate" : "amount";
        renderReturns(lastRows);
      });
    }
  }

  function init() {
    var seed = document.getElementById("sales-returns-data");
    if (!seed) return;
    try {
      renderReturns(JSON.parse(seed.textContent || "[]"));
    } catch (e) {
      renderReturns([]);
    }
  }

  window.salesReturnsInit = init; // يستدعيه وضع المجموعة بعد استبدال لوحة المرتجعات

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
