(function () {
  "use strict";

  var DONUT_COLORS = [
    "#1d6bb8", "#23b268", "#c9891a", "#d45f78", "#6d7f93",
    "#3d7cc4", "#148f52", "#e0a14a", "#a83752", "#445468",
    "#5b8def", "#00897b", "#b0754a", "#2f6fbd", "#9aa8b8"
  ];

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function renderBranchDonut(rows) {
    var board = document.getElementById("purchase-branch-board");
    var empty = document.getElementById("purchase-branch-empty");
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

    var total = 0;
    list.forEach(function (r) {
      total += Math.abs(Number(r.amount) || 0);
    });

    var r = 40;
    var labelR = 40;
    var circ = 2 * Math.PI * r;
    var acc = 0;
    var fracAcc = 0;
    var slices = "";
    var labels = "";

    list.forEach(function (row, i) {
      var val = Math.abs(Number(row.amount) || 0);
      var pct = total > 0 ? val / total : 0;
      var len = pct * circ;
      var color = DONUT_COLORS[i % DONUT_COLORS.length];
      var share = row.share_display || (pct * 100).toFixed(1) + "%";
      slices +=
        '<circle class="branch-donut-slice" cx="50" cy="50" r="' + r + '"' +
        ' fill="none" stroke="' + color + '" stroke-width="18"' +
        ' stroke-dasharray="' + len.toFixed(3) + " " + (circ - len).toFixed(3) + '"' +
        ' stroke-dashoffset="' + (-acc).toFixed(3) + '"' +
        ' data-i="' + i + '">' +
        "<title>" + esc(row.name) + " — " + esc(share) + " — " +
        esc(row.amount_display) + "</title></circle>";

      if (pct >= 0.04) {
        var mid = -Math.PI / 2 + 2 * Math.PI * (fracAcc + pct / 2);
        var lx = 50 + labelR * Math.cos(mid);
        var ly = 50 + labelR * Math.sin(mid);
        labels +=
          '<text class="branch-donut-label" x="' + lx.toFixed(2) + '" y="' + ly.toFixed(2) + '">' +
          esc(String(row.share_pct != null ? row.share_pct : (pct * 100).toFixed(1))) +
          "%</text>";
      }
      acc += len;
      fracAcc += pct;
    });

    var legend = "";
    list.forEach(function (row, i) {
      var color = DONUT_COLORS[i % DONUT_COLORS.length];
      legend +=
        '<li class="branch-donut-legend-item" style="--i: ' + i + '"' +
        ' title="' + esc(row.name) + " — " + esc(row.amount_display) + '">' +
        '<span class="branch-donut-swatch" style="background:' + color + '" aria-hidden="true"></span>' +
        '<span class="branch-donut-legend-body">' +
        '<span class="branch-donut-legend-name">' + esc(row.name) + "</span>" +
        '<span class="branch-donut-legend-meta mono">' +
        esc(row.amount_display) +
        " · " + esc(String(row.invoice_count || 0)) + " فاتورة" +
        " · " + esc(String(row.vendor_count || 0)) + " مورد" +
        "</span></span>" +
        '<span class="branch-donut-pct mono">' + esc(row.share_display || "") + "</span></li>";
    });

    board.innerHTML =
      '<div class="branch-donut-visual">' +
      '<svg class="branch-donut-svg" viewBox="0 0 100 100" role="img" aria-label="توزيع المشتريات حسب الفروع">' +
      '<g transform="rotate(-90 50 50)">' + slices + "</g>" +
      '<circle cx="50" cy="50" r="28" fill="#fff" class="branch-donut-hole"></circle>' +
      labels +
      "</svg></div>" +
      '<ul class="branch-donut-legend" role="list">' + legend + "</ul>";
  }

  function init() {
    var seed = document.getElementById("purchase-branch-data");
    if (!seed) return;
    try {
      renderBranchDonut(JSON.parse(seed.textContent || "[]"));
    } catch (e) {
      renderBranchDonut([]);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
