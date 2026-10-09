(function () {
  "use strict";

  // الفروع الأكثر مرتجعاً: شريط نسبة لكل فرع (يمين) والتفاصيل في الجهة الأخرى (يسار)،
  // وأول 3 فروع ظاهرة مع زر «عرض المزيد».
  var VISIBLE = Infinity; // تُعرض كل الفروع بلا زر «عرض المزيد»

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function renderReturns(rows) {
    var board = document.getElementById("sales-returns-board");
    var empty = document.getElementById("sales-returns-empty");
    var list = rows || [];
    if (!board) return;

    if (!list.length) {
      board.hidden = true;
      board.innerHTML = "";
      if (empty) empty.hidden = false;
      return;
    }

    if (empty) empty.hidden = true;
    board.hidden = false;

    var max = 0;
    list.forEach(function (r) {
      var p = Math.abs(Number(r.share_pct) || 0);
      if (p > max) max = p;
    });

    var html = '<ul class="rb-list" role="list">';
    list.forEach(function (row, i) {
      var pct = Math.abs(Number(row.share_pct) || 0);
      var fill = max > 0 ? Math.max(3, Math.round((pct / max) * 100)) : 0;
      var share = row.share_display || pct.toFixed(1) + "%";
      html +=
        '<li class="rb-row' + (i >= VISIBLE ? " is-extra" : "") + '"' +
        ' title="' + esc(row.name) + " — مرتجع " + esc(row.amount_display) +
        " من إجمالي " + esc(row.gross_total_display || "") + '">' +
        '<div class="rb-bar-col">' +
        '<span class="rb-pct mono">' + esc(share) + "</span>" +
        '<span class="rb-track" aria-hidden="true"><span class="rb-fill" style="width:' + fill + '%"></span></span>' +
        "</div>" +
        '<div class="rb-details">' +
        '<strong class="rb-name">' + esc(row.name) + "</strong>" +
        '<span class="rb-meta mono">' + esc(row.amount_display) + " · " + esc(String(row.invoice_count || 0)) + " مرتجع</span>" +
        "</div></li>";
    });
    html += "</ul>";
    if (list.length > VISIBLE) {
      html +=
        '<button type="button" class="rb-more" aria-expanded="false">' +
        '<span class="rb-more-text">عرض المزيد</span>' +
        '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M15 5l-7 7 7 7"/></svg></button>';
    }
    board.innerHTML = html;

    var btn = board.querySelector(".rb-more");
    if (btn) {
      btn.addEventListener("click", function () {
        var open = board.classList.toggle("is-open");
        btn.setAttribute("aria-expanded", open ? "true" : "false");
        btn.querySelector(".rb-more-text").textContent = open ? "عرض أقل" : "عرض المزيد";
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
