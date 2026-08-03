(function () {
  var panel = document.getElementById("dash-branches");
  var headBtn = document.getElementById("sales-cols-toggle");

  function setExpanded(expanded) {
    if (panel) panel.classList.toggle("is-cols-expanded", expanded);
    document.querySelectorAll(".page.sales-dash .sales-table").forEach(function (table) {
      table.classList.toggle("is-cols-expanded", expanded);
    });
    var label = expanded ? "إخفاء الأعمدة" : "كل الأعمدة";
    var title = expanded ? "إخفاء الأعمدة الإضافية" : "إظهار كل الأعمدة";
    if (headBtn) {
      headBtn.setAttribute("aria-expanded", expanded ? "true" : "false");
      headBtn.textContent = label;
      headBtn.title = title;
    }
    document.querySelectorAll("[data-sales-cols-toggle]").forEach(function (btn) {
      btn.setAttribute("aria-expanded", expanded ? "true" : "false");
      btn.title = title;
    });
  }

  function toggle() {
    var first = document.querySelector(".page.sales-dash .sales-table");
    if (!first) return;
    setExpanded(!first.classList.contains("is-cols-expanded"));
  }

  if (headBtn) headBtn.addEventListener("click", toggle);
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-sales-cols-toggle]");
    if (!btn) return;
    toggle();
  });

  function esc(text) {
    var d = document.createElement("div");
    d.textContent = text == null ? "" : String(text);
    return d.innerHTML;
  }

  function renderGroups(dataPanel) {
    var loading = document.getElementById("groups-loading");
    var table = document.getElementById("sales-groups-table");
    var tbody = document.getElementById("sales-groups-tbody");
    var tfoot = document.getElementById("sales-groups-tfoot");
    var pill = document.getElementById("groups-result-pill");
    var note = document.getElementById("groups-fast-note");
    var panelData = dataPanel || {};
    var rows = panelData.rows || [];
    if (loading) loading.hidden = true;
    if (table) table.hidden = false;
    if (pill) {
      pill.textContent = rows.length + " صف · " + (panelData.grand_invoices_display || "0") + " فاتورة";
    }
    if (note) note.hidden = !panelData.fast_mode;
    if (!tbody) return;
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="10" class="sales-empty">لا توجد مبيعات مجموعات في هذه الفترة.</td></tr>';
      if (tfoot) tfoot.hidden = true;
      return;
    }
    var html = "";
    rows.forEach(function (row, i) {
      var branch =
        panelData.by_branch || panelData.selected_branch
          ? row.branch_name
          : "كل الفروع";
      html +=
        "<tr>" +
        '<td class="mono">' + (i + 1) + "</td>" +
        "<td>" + esc(row.group_name) + "</td>" +
        "<td>" + esc(branch) + "</td>" +
        '<td class="mono"><button type="button" class="sales-num-btn" data-sales-cols-toggle aria-controls="sales-groups-table">' +
        esc(row.invoice_count_display) +
        "</button></td>" +
        '<td class="mono sales-amt">' + esc(row.gross_total_display) + "</td>" +
        '<td class="mono sales-amt">' + esc(row.sales_total_display) + "</td>" +
        '<td class="mono sales-amt">' + esc(row.avg_basket_display) + "</td>" +
        '<td class="mono sales-col-extra">' + esc(row.qty_total_display) + "</td>" +
        '<td class="mono sales-amt sales-col-extra">' + esc(row.net_total_display) + "</td>" +
        '<td class="mono sales-amt sales-col-extra">' + esc(row.vat_total_display) + "</td>" +
        "</tr>";
    });
    tbody.innerHTML = html;
    if (tfoot) {
      tfoot.hidden = false;
      tfoot.innerHTML =
        '<tr class="sales-grand-row">' +
        "<td colspan=\"3\">" + (panelData.by_branch ? "إجمالي المجموعة" : "الإجمالي") + "</td>" +
        '<td class="mono">' + esc(panelData.grand_invoices_display) + "</td>" +
        '<td class="mono sales-amt">' + esc(panelData.grand_gross) + "</td>" +
        '<td class="mono sales-amt">' + esc(panelData.grand_sales) + "</td>" +
        '<td class="mono sales-amt">' + esc(panelData.grand_avg_basket) + "</td>" +
        '<td class="mono sales-col-extra">' + esc(panelData.grand_qty_display) + "</td>" +
        '<td class="mono sales-amt sales-col-extra">' + esc(panelData.grand_net) + "</td>" +
        '<td class="mono sales-amt sales-col-extra">' + esc(panelData.grand_vat) + "</td>" +
        "</tr>";
    }
  }

  function ensureBoard(id, className) {
    var board = document.getElementById(id);
    if (board) return board;
    var loading = document.getElementById(id.replace("-board", "-loading"));
    var parent = loading && loading.parentElement;
    if (!parent) return null;
    board = document.createElement("div");
    board.id = id;
    board.className = className || "branch-chart";
    board.setAttribute("role", "list");
    parent.appendChild(board);
    return board;
  }

  var DONUT_COLORS = [
    "#1d4f91", "#3d7cc4", "#e8b84a", "#c9891a", "#6d7f93",
    "#23b268", "#d45f78", "#5b8def", "#e0a14a", "#148f52",
    "#445468", "#b0754a", "#9aa8b8", "#2f6fbd", "#a83752"
  ];

  function syncReturnFiltersFromBranchChart() {
    var brSrc = document.getElementById("chart-br-branch");
    var grSrc = document.getElementById("chart-br-group");
    var brDst = document.getElementById("chart-ret-branch");
    var grDst = document.getElementById("chart-ret-group");
    if (brSrc && brDst) brDst.value = brSrc.value;
    if (grSrc && grDst) grDst.value = grSrc.value;
  }

  function filterReturnsByBranch(branchCode) {
    var brDst = document.getElementById("chart-ret-branch");
    var grSrc = document.getElementById("chart-br-group");
    var grDst = document.getElementById("chart-ret-group");
    if (brDst) brDst.value = branchCode || "";
    if (grSrc && grDst) grDst.value = grSrc.value;
    loadReturnsChart();
  }

  function bindDonutBranchLinks(board, list) {
    if (!board) return;
    board.querySelectorAll("[data-branch-code]").forEach(function (el) {
      el.addEventListener("click", function () {
        filterReturnsByBranch(el.getAttribute("data-branch-code") || "");
      });
      el.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter" || ev.key === " ") {
          ev.preventDefault();
          filterReturnsByBranch(el.getAttribute("data-branch-code") || "");
        }
      });
    });
  }

  function renderChartBranches(rows) {
    var loading = document.getElementById("chart-branches-loading");
    var empty = document.getElementById("chart-branches-empty");
    var pill = document.getElementById("chart-branches-pill");
    var board = ensureBoard("chart-branches-board", "branch-chart branch-donut");
    var list = rows || [];
    if (loading) loading.hidden = true;
    if (empty) empty.hidden = true;
    if (pill) pill.textContent = "أعلى " + list.length;
    if (!board) return;
    board.hidden = false;
    board.className = "branch-chart branch-donut";
    if (!list.length) {
      board.innerHTML = '<p class="sales-empty">لا توجد مرتجعات فروع لهذه الفلترة.</p>';
      return;
    }

    var total = 0;
    list.forEach(function (b) {
      total += Number(b.sales_total) || 0;
    });

    var r = 40;
    var labelR = 40;
    var circ = 2 * Math.PI * r;
    var acc = 0;
    var fracAcc = 0;
    var slices = "";
    var labels = "";
    list.forEach(function (b, i) {
      var sales = Number(b.sales_total) || 0;
      var pct = total > 0 ? sales / total : 0;
      var len = pct * circ;
      var color = DONUT_COLORS[i % DONUT_COLORS.length];
      var code = esc(b.branch_code || "");
      slices +=
        '<circle class="branch-donut-slice" cx="50" cy="50" r="' + r + '"' +
        ' fill="none" stroke="' + color + '" stroke-width="18"' +
        ' stroke-dasharray="' + len.toFixed(3) + " " + (circ - len).toFixed(3) + '"' +
        ' stroke-dashoffset="' + (-acc).toFixed(3) + '"' +
        ' data-i="' + i + '" data-branch-code="' + code + '" tabindex="0" role="button"' +
        ' style="cursor:pointer">' +
        "<title>" + esc(b.branch_name) + " — " + esc(b.share_pct) + "% — " +
        esc(b.sales_total_display) + " (اضغط لعرض أصناف المرتجع)</title></circle>";

      if (pct >= 0.035) {
        var mid = -Math.PI / 2 + 2 * Math.PI * (fracAcc + pct / 2);
        var lx = 50 + labelR * Math.cos(mid);
        var ly = 50 + labelR * Math.sin(mid);
        labels +=
          '<text class="branch-donut-label" x="' + lx.toFixed(2) + '" y="' + ly.toFixed(2) + '">' +
          esc(b.share_pct) + "%</text>";
      }
      acc += len;
      fracAcc += pct;
    });

    var legend = "";
    list.forEach(function (b, i) {
      var color = DONUT_COLORS[i % DONUT_COLORS.length];
      legend +=
        '<li class="branch-donut-legend-item is-clickable" role="button" tabindex="0" style="--i: ' + i + '"' +
        ' data-branch-code="' + esc(b.branch_code || "") + '"' +
        ' title="' + esc(b.branch_name) + " — " + esc(b.sales_total_display) + ' — عرض أصناف المرتجع">' +
        '<span class="branch-donut-swatch" style="background:' + color + '" aria-hidden="true"></span>' +
        '<span class="branch-donut-legend-body">' +
        '<span class="branch-donut-legend-name">' + esc(b.branch_name) + "</span>" +
        '<span class="branch-donut-legend-meta mono">' +
        esc(b.sales_total_display) + " · " + esc(b.invoice_count_display) + " مرتجع" +
        "</span></span>" +
        '<span class="branch-donut-pct mono">' + esc(b.share_pct) + "%</span></li>";
    });

    board.innerHTML =
      '<div class="branch-donut-visual">' +
      '<svg class="branch-donut-svg" viewBox="0 0 100 100" role="img" aria-label="توزيع مرتجعات الفروع">' +
      '<g transform="rotate(-90 50 50)">' + slices + "</g>" +
      '<circle cx="50" cy="50" r="28" fill="#fff" class="branch-donut-hole"></circle>' +
      labels +
      "</svg></div>" +
      '<ul class="branch-donut-legend" role="list">' + legend + "</ul>";
    bindDonutBranchLinks(board, list);
  }

  function renderReturnItems(items) {
    var chartLoading = document.getElementById("chart-items-loading");
    var pill = document.getElementById("chart-returns-pill");
    var chartBoard = ensureBoard("chart-items-board", "branch-chart top20-chart");
    var list = items || [];
    if (chartLoading) chartLoading.hidden = true;
    if (pill) pill.textContent = "أعلى " + list.length;
    if (!chartBoard) return;
    chartBoard.hidden = false;
    if (!list.length) {
      chartBoard.innerHTML = '<p class="sales-empty">لا توجد مرتجعات لهذه الفلترة.</p>';
      return;
    }
    var chartHtml = "";
    list.forEach(function (item, i) {
      var amt = item.return_total_display || item.sales_total_display;
      chartHtml +=
        '<div class="branch-chart-row is-return" role="listitem" style="--bar-pct: ' +
        esc(item.share_pct) + '%; --i: ' + i + '" title="' +
        esc(item.item_name) + " — " + esc(amt) + '">' +
        '<span class="branch-chart-rank mono" aria-hidden="true">' + (i + 1) + "</span>" +
        '<span class="branch-chart-name">' + esc(item.item_name) + "</span>" +
        '<span class="branch-chart-track" aria-hidden="true"><span class="branch-chart-fill"></span></span>' +
        '<span class="branch-chart-meta">' +
        '<span class="branch-chart-amt mono">' + esc(amt) + "</span>" +
        '<span class="branch-chart-inv mono">' + esc(item.qty_total_display) + " كمية مرتجعة</span>" +
        "</span></div>";
    });
    chartBoard.innerHTML = chartHtml;
  }

  function renderItems(items) {
    var loading = document.getElementById("items-loading");
    var board = document.getElementById("items-board");
    var pill = document.getElementById("items-pill");
    var list = items || [];

    if (loading) loading.hidden = true;
    if (pill) pill.textContent = "أعلى " + (list.length || 20);
    if (!board) return;
    board.hidden = false;
    if (!list.length) {
      board.innerHTML = '<p class="sales-empty">لا توجد أصناف في هذه الفترة.</p>';
      return;
    }
    var html = "";
    list.forEach(function (item, i) {
      var rank = i + 1;
      html +=
        '<div class="seller-card rank-' + rank + '" role="listitem" style="--bar-pct: ' +
        esc(item.share_pct) + '%; --i: ' + i + '">' +
        '<span class="seller-rank mono" aria-hidden="true">' + rank + "</span>" +
        '<span class="seller-body">' +
        '<span class="seller-name" title="' + esc(item.item_name) + " — " + esc(item.item_code) + '">' +
        esc(item.item_name) +
        "</span>" +
        '<span class="seller-meta mono">' +
        esc(item.qty_total_display) +
        " كمية · " +
        esc(item.invoice_count) +
        " فاتورة</span>" +
        '<span class="seller-track" aria-hidden="true"><span class="seller-fill"></span></span>' +
        "</span>" +
        '<span class="seller-side">' +
        '<span class="seller-amt mono">' + esc(item.sales_total_display) + "</span>" +
        '<span class="seller-pct mono">' + esc(item.share_pct) + "%</span>" +
        "</span></div>";
    });
    board.innerHTML = html;
  }

  function sellerInvoiceHref(userCode, branchCode) {
    var params = new URLSearchParams(window.location.search);
    if (branchCode) params.set("branch", branchCode);
    else params.delete("branch");
    params.set("user_id", userCode);
    return "?" + params.toString() + "#invoices";
  }

  function renderSellers(users) {
    var loading = document.getElementById("sellers-loading");
    var board = document.getElementById("sellers-board");
    var pill = document.getElementById("sellers-pill");
    var branchSel = document.getElementById("side-sellers-branch");
    var branchCode = branchSel && branchSel.value ? branchSel.value : "";
    var list = users || [];
    if (loading) loading.hidden = true;
    if (pill) pill.textContent = "أعلى " + (list.length || 8);
    if (!board) return;
    if (!list.length) {
      board.innerHTML = '<p class="sales-empty">لا يوجد مستخدمون في هذه الفترة.</p>';
      return;
    }
    var html = "";
    list.forEach(function (u, i) {
      var rank = i + 1;
      html +=
        '<a class="seller-card rank-' + rank + '" href="' +
        esc(sellerInvoiceHref(u.user_code, branchCode)) +
        '" role="listitem" style="--bar-pct: ' + esc(u.share_pct) + "%; --i: " + i +
        '" data-user-code="' + esc(u.user_code) + '">' +
        '<span class="seller-rank mono" aria-hidden="true">' + rank + "</span>" +
        '<span class="seller-body">' +
        '<span class="seller-name" title="' + esc(u.user_name) + " — " + esc(u.user_code) + '">' +
        esc(u.user_name) +
        "</span>" +
        '<span class="seller-meta mono">' +
        esc(u.invoice_count) +
        " فاتورة · #" +
        esc(u.user_code) +
        "</span>" +
        '<span class="seller-track" aria-hidden="true"><span class="seller-fill"></span></span>' +
        "</span>" +
        '<span class="seller-side">' +
        '<span class="seller-amt mono">' + esc(u.sales_total_display) + "</span>" +
        '<span class="seller-pct mono">' + esc(u.share_pct) + "%</span>" +
        "</span></a>";
    });
    board.innerHTML = html;
  }

  function sideItemsUrl() {
    var box = document.getElementById("dash-top-items");
    if (!box || !box.dataset.itemsUrl) return "";
    var url = box.dataset.itemsUrl;
    var branchSel = document.getElementById("side-items-branch");
    if (branchSel && branchSel.value) url += "&branch=" + encodeURIComponent(branchSel.value);
    return url;
  }

  function sideUsersUrl() {
    var box = document.getElementById("dash-top-sellers");
    if (!box || !box.dataset.usersUrl) return "";
    var url = box.dataset.usersUrl;
    var branchSel = document.getElementById("side-sellers-branch");
    if (branchSel && branchSel.value) url += "&branch=" + encodeURIComponent(branchSel.value);
    return url;
  }

  function loadSideItems() {
    var url = sideItemsUrl();
    if (!url) return;
    var loading = document.getElementById("items-loading");
    var board = document.getElementById("items-board");
    if (loading) {
      loading.hidden = false;
      loading.textContent = "جاري تحميل الأصناف…";
    }
    if (board) board.hidden = true;
    fetch(url, { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data.ok) throw new Error(data.error || "فشل التحميل");
        renderItems(data.items || []);
      })
      .catch(function (err) {
        if (loading) loading.textContent = "تعذّر تحميل الأصناف: " + (err.message || err);
      });
  }

  function loadSideSellers() {
    var url = sideUsersUrl();
    if (!url) return;
    var loading = document.getElementById("sellers-loading");
    if (loading) {
      loading.hidden = false;
      loading.textContent = "جاري تحميل البائعين…";
    }
    fetch(url, { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data.ok) throw new Error(data.error || "فشل التحميل");
        renderSellers(data.users || []);
      })
      .catch(function (err) {
        if (loading) loading.textContent = "تعذّر تحميل البائعين: " + (err.message || err);
      });
  }

  function failGroups(err) {
    var loading = document.getElementById("groups-loading");
    if (loading) loading.textContent = "تعذّر تحميل المجموعات: " + (err.message || err);
  }

  function failItems(err) {
    var loading = document.getElementById("items-loading");
    if (loading) loading.textContent = "تعذّر تحميل الأصناف: " + (err.message || err);
    var chartLoading = document.getElementById("chart-items-loading");
    if (chartLoading) chartLoading.textContent = "تعذّر تحميل المرتجعات: " + (err.message || err);
  }

  function chartsUrl(branchSel, groupSel) {
    var box = document.getElementById("dash-charts");
    if (!box || !box.dataset.chartsUrl) return "";
    var url = box.dataset.chartsUrl;
    if (branchSel && branchSel.value) url += "&branch=" + encodeURIComponent(branchSel.value);
    if (groupSel && groupSel.value) url += "&group=" + encodeURIComponent(groupSel.value);
    return url;
  }

  function loadBranchChart() {
    syncReturnFiltersFromBranchChart();
    var url = chartsUrl(
      document.getElementById("chart-br-branch"),
      document.getElementById("chart-br-group")
    );
    if (!url) return;
    var loadingBr = document.getElementById("chart-branches-loading");
    var loadingIt = document.getElementById("chart-items-loading");
    if (loadingBr) {
      loadingBr.hidden = false;
      loadingBr.textContent = "جاري تحديث الفروع…";
    }
    if (loadingIt) {
      loadingIt.hidden = false;
      loadingIt.textContent = "جاري تحديث المرتجعات…";
    }
    fetch(url, { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data.ok) throw new Error(data.error || "فشل التحميل");
        renderChartBranches(data.chart_branches || []);
        renderReturnItems(data.return_items || []);
      })
      .catch(function (err) {
        if (loadingBr) loadingBr.textContent = "تعذّر تحديث الفروع: " + (err.message || err);
        if (loadingIt) loadingIt.textContent = "تعذّر تحديث المرتجعات: " + (err.message || err);
      });
  }

  function loadReturnsChart() {
    var url = chartsUrl(
      document.getElementById("chart-ret-branch"),
      document.getElementById("chart-ret-group")
    );
    if (!url) return;
    var loadingIt = document.getElementById("chart-items-loading");
    if (loadingIt) {
      loadingIt.hidden = false;
      loadingIt.textContent = "جاري تحديث المرتجعات…";
    }
    fetch(url, { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data.ok) throw new Error(data.error || "فشل التحميل");
        renderReturnItems(data.return_items || []);
      })
      .catch(function (err) {
        if (loadingIt) loadingIt.textContent = "تعذّر تحديث المرتجعات: " + (err.message || err);
      });
  }

  function loadPanels() {
    var groupsBox = document.getElementById("dash-groups");
    var itemsBox = document.getElementById("dash-top-items");
    var panelsUrl =
      (groupsBox && groupsBox.dataset.panelsUrl) ||
      (itemsBox && itemsBox.dataset.panelsUrl) ||
      "";
    if (!panelsUrl) return false;

    fetch(panelsUrl, { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data.ok) throw new Error(data.error || "فشل التحميل");
        renderGroups(data.panel || {});
        renderItems(data.items || []);
        renderReturnItems(data.return_items || []);
      })
      .catch(function (err) {
        failGroups(err);
        failItems(err);
      });
    return true;
  }

  function marginTone(pct) {
    if (pct == null || isNaN(Number(pct))) return "";
    var n = Number(pct);
    if (n >= 25) return "is-up";
    if (n < 0) return "is-down";
    if (n < 10) return "is-down";
    return "";
  }

  function fmtMarginMoney(n) {
    var x = Number(n);
    if (!isFinite(x)) return "0.00";
    return x.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function marginTotals(rows) {
    var sales = 0;
    var cost = 0;
    var profit = 0;
    (rows || []).forEach(function (row) {
      sales += Number(row.sales_net) || 0;
      cost += Number(row.cost_total) || 0;
      profit += Number(row.profit) || 0;
    });
    var pct = cost > 0 ? Math.round((profit / cost) * 10000) / 100 : null;
    return {
      sales_net: sales,
      cost_total: cost,
      profit: profit,
      margin_pct: pct,
      sales_net_display: fmtMarginMoney(sales),
      cost_total_display: fmtMarginMoney(cost),
      profit_display: fmtMarginMoney(profit),
      margin_pct_display: pct == null ? "—" : fmtMarginMoney(pct) + "%"
    };
  }

  function renderMarginTotals(totalsId, totals) {
    var box = document.getElementById(totalsId);
    if (!box) return;
    if (!totals) {
      box.innerHTML = "";
      box.hidden = true;
      return;
    }
    var tone = marginTone(totals.margin_pct);
    box.hidden = false;
    box.innerHTML =
      '<table class="data-table sales-table margin-totals-table" aria-hidden="true">' +
      "<colgroup>" +
      '<col class="m-col-idx" />' +
      '<col class="m-col-name" />' +
      '<col class="m-col-num" />' +
      '<col class="m-col-num" />' +
      '<col class="m-col-num" />' +
      '<col class="m-col-num" />' +
      "</colgroup><tbody>" +
      '<tr class="sales-grand-row">' +
      '<td colspan="2">الإجمالي</td>' +
      '<td class="mono sales-amt">' + esc(totals.sales_net_display) + "</td>" +
      '<td class="mono">' + esc(totals.cost_total_display) + "</td>" +
      '<td class="mono ' + tone + '">' + esc(totals.profit_display) + "</td>" +
      '<td class="mono ' + tone + '">' + esc(totals.margin_pct_display) + "</td>" +
      "</tr></tbody></table>";
  }

  function renderMarginBranches(rows) {
    var loading = document.getElementById("margin-branches-loading");
    var block = document.getElementById("margin-branches-block");
    var wrap = document.getElementById("margin-branches-wrap");
    var empty = document.getElementById("margin-branches-empty");
    var body = document.getElementById("margin-branches-body");
    var pill = document.getElementById("margin-branches-pill");
    var list = rows || [];
    if (loading) loading.hidden = true;
    if (pill) pill.textContent = list.length ? (list.length + " فرع") : "كل الفروع";
    if (!body) return;
    if (!list.length) {
      if (block) block.hidden = true;
      if (wrap) wrap.hidden = true;
      if (empty) empty.hidden = false;
      body.innerHTML = "";
      renderMarginTotals("margin-branches-totals", null);
      return;
    }
    if (empty) empty.hidden = true;
    if (block) block.hidden = false;
    if (wrap) wrap.hidden = false;
    body.innerHTML = list.map(function (row, i) {
      var tone = marginTone(row.margin_pct);
      return (
        "<tr>" +
        '<td class="mono">' + (i + 1) + "</td>" +
        "<td title=\"" + esc(row.branch_code || "") + "\">" + esc(row.branch_name || row.branch_code || "") + "</td>" +
        '<td class="mono sales-amt">' + esc(row.sales_net_display || "0.00") + "</td>" +
        '<td class="mono">' + esc(row.cost_total_display || "0.00") + "</td>" +
        '<td class="mono ' + tone + '">' + esc(row.profit_display || "0.00") + "</td>" +
        '<td class="mono ' + tone + '">' + esc(row.margin_pct_display || "—") + "</td>" +
        "</tr>"
      );
    }).join("");
    renderMarginTotals("margin-branches-totals", marginTotals(list));
  }

  function renderMarginGroups(rows) {
    var loading = document.getElementById("margin-groups-loading");
    var block = document.getElementById("margin-groups-block");
    var wrap = document.getElementById("margin-groups-wrap");
    var empty = document.getElementById("margin-groups-empty");
    var body = document.getElementById("margin-groups-body");
    var pill = document.getElementById("margin-groups-pill");
    var list = rows || [];
    if (loading) loading.hidden = true;
    if (pill) pill.textContent = list.length ? (list.length + " مجموعة") : "كل المجموعات";
    if (!body) return;
    if (!list.length) {
      if (block) block.hidden = true;
      if (wrap) wrap.hidden = true;
      if (empty) empty.hidden = false;
      body.innerHTML = "";
      renderMarginTotals("margin-groups-totals", null);
      return;
    }
    if (empty) empty.hidden = true;
    if (block) block.hidden = false;
    if (wrap) wrap.hidden = false;
    body.innerHTML = list.map(function (row, i) {
      var tone = marginTone(row.margin_pct);
      return (
        "<tr>" +
        '<td class="mono">' + (i + 1) + "</td>" +
        "<td title=\"" + esc(row.group_code || "") + "\">" + esc(row.group_name || row.group_code || "") + "</td>" +
        '<td class="mono sales-amt">' + esc(row.sales_net_display || "0.00") + "</td>" +
        '<td class="mono">' + esc(row.cost_total_display || "0.00") + "</td>" +
        '<td class="mono ' + tone + '">' + esc(row.profit_display || "0.00") + "</td>" +
        '<td class="mono ' + tone + '">' + esc(row.margin_pct_display || "—") + "</td>" +
        "</tr>"
      );
    }).join("");
    renderMarginTotals("margin-groups-totals", marginTotals(list));
  }

  function marginsUrl(branchSel, groupSel) {
    var box = document.getElementById("dash-margins");
    if (!box || !box.dataset.marginsUrl) return "";
    var url = box.dataset.marginsUrl;
    if (branchSel && branchSel.value) url += "&branch=" + encodeURIComponent(branchSel.value);
    if (groupSel && groupSel.value) url += "&group=" + encodeURIComponent(groupSel.value);
    return url;
  }

  function loadMarginBranches() {
    var url = marginsUrl(
      document.getElementById("margin-br-branch"),
      document.getElementById("margin-br-group")
    );
    if (!url) return;
    var loading = document.getElementById("margin-branches-loading");
    if (loading) {
      loading.hidden = false;
      loading.textContent = "جاري تحميل هامش الفروع…";
    }
    fetch(url, { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data.ok) throw new Error(data.error || "فشل التحميل");
        renderMarginBranches(data.branches || []);
      })
      .catch(function (err) {
        if (loading) loading.textContent = "تعذّر تحميل هامش الفروع: " + (err.message || err);
      });
  }

  function loadMarginGroups() {
    var url = marginsUrl(
      document.getElementById("margin-gr-branch"),
      document.getElementById("margin-gr-group")
    );
    if (!url) return;
    var loading = document.getElementById("margin-groups-loading");
    if (loading) {
      loading.hidden = false;
      loading.textContent = "جاري تحميل هامش المجموعات…";
    }
    fetch(url, { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data.ok) throw new Error(data.error || "فشل التحميل");
        renderMarginGroups(data.groups || []);
      })
      .catch(function (err) {
        if (loading) loading.textContent = "تعذّر تحميل هامش المجموعات: " + (err.message || err);
      });
  }

  function loadMargins() {
    var box = document.getElementById("dash-margins");
    if (!box || !box.dataset.marginsUrl) return;
    // طلب واحد للفروع والمجموعات (نفس الحمولة) — أسرع من طلبين متوازيين
    var brSel = document.getElementById("margin-br-branch");
    var brGrp = document.getElementById("margin-br-group");
    var grSel = document.getElementById("margin-gr-branch");
    var grGrp = document.getElementById("margin-gr-group");
    var sameFilters =
      (!brSel || !grSel || brSel.value === grSel.value) &&
      (!brGrp || !grGrp || brGrp.value === grGrp.value);
    if (sameFilters) {
      var url = marginsUrl(brSel || grSel, brGrp || grGrp);
      if (!url) return;
      var loadingBranches = document.getElementById("margin-branches-loading");
      var loadingGroups = document.getElementById("margin-groups-loading");
      if (loadingBranches) {
        loadingBranches.hidden = false;
        loadingBranches.textContent = "جاري تحميل هامش الفروع…";
      }
      if (loadingGroups) {
        loadingGroups.hidden = false;
        loadingGroups.textContent = "جاري تحميل هامش المجموعات…";
      }
      fetch(url, { credentials: "same-origin" })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (!data.ok) throw new Error(data.error || "فشل التحميل");
          renderMarginBranches(data.branches || []);
          renderMarginGroups(data.groups || []);
        })
        .catch(function (err) {
          if (loadingBranches) loadingBranches.textContent = "تعذّر تحميل هامش الفروع: " + (err.message || err);
          if (loadingGroups) loadingGroups.textContent = "تعذّر تحميل هامش المجموعات: " + (err.message || err);
        });
      return;
    }
    loadMarginBranches();
    loadMarginGroups();
  }

  document.querySelectorAll("[data-chart-apply]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      if (btn.getAttribute("data-chart-apply") === "branches") loadBranchChart();
      else loadReturnsChart();
    });
  });

  document.querySelectorAll("[data-side-apply]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      if (btn.getAttribute("data-side-apply") === "items") loadSideItems();
      else loadSideSellers();
    });
  });

  document.querySelectorAll("[data-margin-apply]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      if (btn.getAttribute("data-margin-apply") === "branches") loadMarginBranches();
      else loadMarginGroups();
    });
  });

  var seedEl = document.getElementById("chart-branches-seed");
  if (seedEl) {
    try {
      renderChartBranches(JSON.parse(seedEl.textContent || "[]"));
    } catch (e) { /* keep server HTML if any */ }
  }

  loadMargins();
  if (!loadPanels()) {
    var box = document.getElementById("dash-groups");
    if (box && box.dataset.groupsUrl) {
      fetch(box.dataset.groupsUrl, { credentials: "same-origin" })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (!data.ok) throw new Error(data.error || "فشل التحميل");
          renderGroups(data.panel || {});
        })
        .catch(failGroups);
    }
    var itemsBox = document.getElementById("dash-top-items");
    if (itemsBox && itemsBox.dataset.itemsUrl) {
      fetch(itemsBox.dataset.itemsUrl, { credentials: "same-origin" })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (!data.ok) throw new Error(data.error || "فشل التحميل");
          renderItems(data.items || []);
        })
        .catch(failItems);
    }
    loadReturnsChart();
  }
})();
