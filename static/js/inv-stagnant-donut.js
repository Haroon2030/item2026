(function () {
  "use strict";

  var DONUT_COLORS = [
    "#c9891a", "#d45f78", "#6d7f93", "#1d4f91", "#e8b84a",
    "#445468", "#b0754a", "#3d7cc4", "#a83752", "#23b268",
    "#5b8def", "#148f52", "#9aa8b8", "#2f6fbd", "#e0a14a"
  ];

  var SALES_COLORS = [
    "#1d4f91", "#2f6fbd", "#23b268", "#148f52", "#3d7cc4",
    "#5b8def", "#c9891a", "#e8b84a", "#14b8a6", "#0f766e",
    "#6d7f93", "#445468", "#d45f78", "#b0754a", "#9aa8b8"
  ];

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function renderInvDonut(opts) {
    var board = document.getElementById(opts.boardId);
    var empty = document.getElementById(opts.emptyId);
    var pill = document.getElementById(opts.pillId);
    var list = opts.rows || [];
    var valueKey = opts.valueKey || "stock_value";
    var displayKey = opts.displayKey || "stock_value_display";
    var colors = opts.colors || DONUT_COLORS;
    if (!board) return;

    if (pill && typeof opts.pillText === "function") {
      pill.textContent = opts.pillText(list.length);
    }

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
      total += Number(r[valueKey]) || 0;
    });

    var r = 40;
    var labelR = 40;
    var circ = 2 * Math.PI * r;
    var acc = 0;
    var fracAcc = 0;
    var slices = "";
    var labels = "";

    list.forEach(function (row, i) {
      var val = Number(row[valueKey]) || 0;
      var pct = total > 0 ? val / total : 0;
      var len = pct * circ;
      var color = colors[i % colors.length];
      var sharePct = Math.round(pct * 1000) / 10;
      var share = sharePct.toFixed(1) + "%";
      var display = row[displayKey] || "";
      slices +=
        '<circle class="branch-donut-slice" cx="50" cy="50" r="' + r + '"' +
        ' fill="none" stroke="' + color + '" stroke-width="18"' +
        ' stroke-dasharray="' + len.toFixed(3) + " " + (circ - len).toFixed(3) + '"' +
        ' stroke-dashoffset="' + (-acc).toFixed(3) + '"' +
        ' data-i="' + i + '">' +
        "<title>" + esc(row.name) + " — " + esc(share) + " — " +
        esc(display) + "</title></circle>";

      if (pct >= 0.035) {
        var mid = -Math.PI / 2 + 2 * Math.PI * (fracAcc + pct / 2);
        var lx = 50 + labelR * Math.cos(mid);
        var ly = 50 + labelR * Math.sin(mid);
        labels +=
          '<text class="branch-donut-label" x="' + lx.toFixed(2) + '" y="' + ly.toFixed(2) + '">' +
          esc(String(sharePct)) +
          "%</text>";
      }
      acc += len;
      fracAcc += pct;
    });

    var legend = "";
    list.forEach(function (row, i) {
      var val = Number(row[valueKey]) || 0;
      var pct = total > 0 ? val / total : 0;
      var sharePct = Math.round(pct * 1000) / 10;
      var share = sharePct.toFixed(1) + "%";
      var color = colors[i % colors.length];
      var meta = typeof opts.metaText === "function"
        ? opts.metaText(row)
        : String(row[displayKey] || "");
      legend +=
        '<li class="branch-donut-legend-item" style="--i: ' + i + '"' +
        ' title="' + esc(row.name) + " — " + esc(row[displayKey] || "") + '">' +
        '<span class="branch-donut-swatch" style="background:' + color + '" aria-hidden="true"></span>' +
        '<span class="branch-donut-legend-body">' +
        '<span class="branch-donut-legend-name">' + esc(row.name) + "</span>" +
        '<span class="branch-donut-legend-meta mono">' + esc(meta) + "</span></span>" +
        '<span class="branch-donut-pct mono">' + esc(share) + "</span></li>";
    });

    board.innerHTML =
      '<div class="branch-donut-visual">' +
      '<svg class="branch-donut-svg" viewBox="0 0 100 100" role="img" aria-label="' +
      esc(opts.ariaLabel || "توزيع") + '">' +
      '<g transform="rotate(-90 50 50)">' + slices + "</g>" +
      '<circle cx="50" cy="50" r="28" fill="#fff" class="branch-donut-hole"></circle>' +
      labels +
      "</svg></div>" +
      '<ul class="branch-donut-legend" role="list">' + legend + "</ul>";
  }

  function parseSeed(id) {
    var seed = document.getElementById(id);
    if (!seed) return null;
    try {
      return JSON.parse(seed.textContent || "[]");
    } catch (e) {
      return [];
    }
  }

  function init() {
    var stagnant = parseSeed("inv-stagnant-data");
    if (stagnant !== null) {
      renderInvDonut({
        boardId: "inv-stagnant-board",
        emptyId: "inv-stagnant-empty",
        pillId: "inv-stagnant-pill",
        rows: stagnant,
        valueKey: "qty_total",
        displayKey: "qty_display",
        colors: DONUT_COLORS,
        ariaLabel: "أصناف راكدة بأعلى كمية",
        pillText: function (n) {
          return n ? "أعلى " + n : "0 صنف";
        },
        metaText: function (row) {
          return (
            "كمية " + (row.qty_display || "0") +
            " · قيمة " + (row.stock_value_display || "0") +
            (row.code ? " · #" + row.code : "")
          );
        }
      });
    }

    var sales = parseSeed("inv-group-sales-data");
    if (sales !== null) {
      renderInvDonut({
        boardId: "inv-group-sales-board",
        emptyId: "inv-group-sales-empty",
        pillId: "inv-group-sales-pill",
        rows: (sales || []).slice(0, 10),
        valueKey: "sales_total",
        displayKey: "sales_display",
        colors: SALES_COLORS,
        ariaLabel: "المجموعات الأكثر مبيعات",
        pillText: function (n) {
          return n ? "أعلى " + n : "0 مجموعة";
        },
        metaText: function (row) {
          return (
            "مبيعات " + (row.sales_display || "0") +
            " · كمية " + (row.qty_display || "0") +
            " · " + (row.turnover_display || "—") +
            " · مخزون " + (row.stock_value_display || "—")
          );
        }
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
