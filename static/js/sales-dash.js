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

  function dashProgressPct(done, total) {
    var t = Number(total) || 0;
    var d = Number(done) || 0;
    if (t <= 0) return 0;
    if (d <= 0) return 0;
    if (d >= t) return 100;
    var pct = Math.round((d / t) * 100);
    return Math.max(1, Math.min(99, pct));
  }

  function setDashLoadProgress(el, done, total, label) {
    if (!el) return;
    var pct = dashProgressPct(done, total);
    el.hidden = false;
    el.classList.add("dash-load-progress");
    el.innerHTML =
      '<span class="dash-load-msg">' + esc(label || "جاري التحميل…") + "</span>" +
      '<span class="dash-load-row">' +
      '<span class="dash-load-track" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="' +
      pct +
      '" aria-label="' +
      esc(label || "التقدم") +
      '">' +
      '<span class="dash-load-fill" style="width:' + pct + '%"></span>' +
      "</span>" +
      '<span class="dash-load-pct mono">' + pct + "%</span>" +
      "</span>";
  }

  function clearDashLoadProgress(el) {
    if (!el) return;
    el.classList.remove("dash-load-progress");
    el.innerHTML = "";
    el.hidden = true;
  }

  var SAR_SVG =
    '<svg class="sar-symbol" viewBox="0 0 1124.14 1256.39" aria-label="ريال سعودي" role="img" focusable="false">' +
    '<path fill="currentColor" d="M699.62,1113.02h0c-20.06,44.48-33.32,92.75-38.4,143.37l424.51-90.24c20.06-44.47,33.31-92.75,38.4-143.37l-424.51,90.24Z"/>' +
    '<path fill="currentColor" d="M1085.73,895.8c20.06-44.47,33.32-92.75,38.4-143.37l-330.68,70.33v-135.2l292.27-62.11c20.06-44.47,33.32-92.75,38.4-143.37l-330.68,70.27V66.13c-50.67,28.45-95.67,66.32-132.25,110.99v403.35l-123.31,26.15V0c-50.67,28.44-95.67,66.32-132.25,110.99v525.69l-295.91,62.83c-20.06,44.47-33.33,92.75-38.42,143.37l334.33-71.05v170.26l-358.3,76.14c-20.06,44.47-33.32,92.75-38.4,143.37l375.04-79.7c30.53-6.35,56.77-24.4,73.83-50.9l36.68-30.52v92.57l-123.31,26.15v-92.57l36.68,30.52c17.06,26.5,43.3,44.55,73.83,50.9l375.04,79.7Z"/>' +
    "</svg>";

  function moneyHtml(amount, className) {
    className = className || "money";
    return (
      '<span class="' +
      className +
      '"><span class="money-value">' +
      esc(amount) +
      "</span>" +
      SAR_SVG +
      "</span>"
    );
  }

  function moneyCell(amount, extraClass) {
    var cls = "mono sales-amt" + (extraClass ? " " + extraClass : "");
    return '<td class="' + cls + '">' + moneyHtml(amount) + "</td>";
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
    if (loading) {
      if (panelData.progressive_done === false) {
        loading.hidden = false;
      } else {
        clearDashLoadProgress(loading);
      }
    }
    if (table) table.hidden = false;
    if (pill) {
      var invPart = panelData.fast_mode
        ? (panelData.rows || []).length + " مجموعة"
        : rows.length + " صف · " + (panelData.grand_invoices_display || "0") + " فاتورة";
      if (panelData.progressive_pill) invPart += " · " + panelData.progressive_pill;
      pill.textContent = invPart;
    }
    if (note) note.hidden = false;
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
        moneyCell(row.gross_total_display, "sales-amt-gross") +
        moneyCell(row.sales_total_display, "sales-amt-net") +
        moneyCell(row.avg_basket_display, "sales-amt-avg") +
        '<td class="mono sales-col-extra">' + esc(row.qty_total_display) + "</td>" +
        moneyCell(row.net_total_display, "sales-col-extra") +
        moneyCell(row.vat_total_display, "sales-col-extra") +
        "</tr>";
    });
    tbody.innerHTML = html;
    if (tfoot) {
      tfoot.hidden = false;
      tfoot.innerHTML =
        '<tr class="sales-grand-row">' +
        "<td colspan=\"3\">" + (panelData.by_branch ? "إجمالي المجموعة" : "الإجمالي") + "</td>" +
        '<td class="mono">' + esc(panelData.grand_invoices_display) + "</td>" +
        moneyCell(panelData.grand_gross, "sales-amt-gross") +
        moneyCell(panelData.grand_sales, "sales-amt-net") +
        moneyCell(panelData.grand_avg_basket, "sales-amt-avg") +
        '<td class="mono sales-col-extra">' + esc(panelData.grand_qty_display) + "</td>" +
        moneyCell(panelData.grand_net, "sales-col-extra") +
        moneyCell(panelData.grand_vat, "sales-col-extra") +
        "</tr>";
    }
  }

  function fmtMoneyNum(n) {
    var x = Number(n);
    if (!isFinite(x)) return "0.00";
    return x.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function monthChunksNewestFirst(fromStr, toStr) {
    var partsFrom = String(fromStr || "").split("-");
    var partsTo = String(toStr || "").split("-");
    if (partsFrom.length < 3 || partsTo.length < 3) return [];
    var y1 = +partsFrom[0], m1 = +partsFrom[1], d1 = +partsFrom[2];
    var y2 = +partsTo[0], m2 = +partsTo[1], d2 = +partsTo[2];
    if (!y1 || !m1 || !d1 || !y2 || !m2 || !d2) return [];
    function pad(n) { return n < 10 ? "0" + n : String(n); }
    function lastDay(y, m) { return new Date(y, m, 0).getDate(); }
    function iso(y, m, d) { return y + "-" + pad(m) + "-" + pad(d); }
    var chunks = [];
    var y = y1, m = m1;
    while (y < y2 || (y === y2 && m <= m2)) {
      var startD = (y === y1 && m === m1) ? d1 : 1;
      var endD = (y === y2 && m === m2) ? d2 : lastDay(y, m);
      chunks.push({ date_from: iso(y, m, startD), date_to: iso(y, m, endD) });
      m += 1;
      if (m > 12) { m = 1; y += 1; }
    }
    chunks.reverse();
    return chunks;
  }

  /** شرائح هامش: أشهر حديثة أولاً؛ للفترات الطويلة تُقسَم الأشهر الثقيلة إلى نصفين. */
  function marginChunksNewestFirst(fromStr, toStr) {
    var months = monthChunksNewestFirst(fromStr, toStr);
    if (months.length < 2) return months;
    function pad(n) { return n < 10 ? "0" + n : String(n); }
    function parseIso(s) {
      var p = String(s || "").split("-");
      return { y: +p[0], m: +p[1], d: +p[2] };
    }
    function toIso(o) { return o.y + "-" + pad(o.m) + "-" + pad(o.d); }
    function daysIn(a, b) {
      var A = parseIso(a), B = parseIso(b);
      var t0 = Date.UTC(A.y, A.m - 1, A.d);
      var t1 = Date.UTC(B.y, B.m - 1, B.d);
      return Math.round((t1 - t0) / 86400000) + 1;
    }
    function addDays(isoStr, n) {
      var A = parseIso(isoStr);
      var dt = new Date(Date.UTC(A.y, A.m - 1, A.d));
      dt.setUTCDate(dt.getUTCDate() + n);
      return toIso({ y: dt.getUTCFullYear(), m: dt.getUTCMonth() + 1, d: dt.getUTCDate() });
    }
    var out = [];
    months.forEach(function (ch) {
      var span = daysIn(ch.date_from, ch.date_to);
      if (span <= 16) {
        out.push(ch);
        return;
      }
      var midEnd = addDays(ch.date_from, Math.ceil(span / 2) - 1);
      var midStart = addDays(midEnd, 1);
      // الأحدث أولاً داخل الشهر
      out.push({ date_from: midStart, date_to: ch.date_to });
      out.push({ date_from: ch.date_from, date_to: midEnd });
    });
    return out;
  }

  function delayMs(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
  }

  /** جلب JSON مع مهلة وإعادة محاولة — يقلل فشل الشهور الثقيلة في الإنتاج. */
  function fetchJsonRetry(url, opts) {
    opts = opts || {};
    var attempts = opts.attempts == null ? 3 : opts.attempts;
    var timeoutMs = opts.timeoutMs == null ? 150000 : opts.timeoutMs;
    var n = 0;
    function once() {
      n += 1;
      var ctrl = typeof AbortController !== "undefined" ? new AbortController() : null;
      var timer = null;
      if (ctrl) {
        timer = setTimeout(function () { try { ctrl.abort(); } catch (e) { /* ignore */ } }, timeoutMs);
      }
      return fetch(url, {
        credentials: "same-origin",
        signal: ctrl ? ctrl.signal : undefined
      })
        .then(function (r) {
          if (!r.ok) throw new Error("HTTP " + r.status);
          return r.json();
        })
        .finally(function () {
          if (timer) clearTimeout(timer);
        })
        .catch(function (err) {
          if (n >= attempts) throw err;
          return delayMs(700 * n).then(once);
        });
    }
    return once();
  }

  function groupRowKey(row, byBranch) {
    return String(row.group_code || "") + "|" + (byBranch ? String(row.branch_code || "") : "");
  }

  function mergeGroupPanels(basePanel, nextPanel) {
    var byBranch = !!(basePanel.by_branch || nextPanel.by_branch);
    var map = {};
    function ingest(panel) {
      (panel.rows || []).forEach(function (row) {
        var k = groupRowKey(row, byBranch);
        var cur = map[k];
        if (!cur) {
          map[k] = {
            group_code: row.group_code,
            group_name: row.group_name,
            branch_code: row.branch_code,
            branch_name: row.branch_name,
            invoice_count: Number(row.invoice_count) || 0,
            return_count: Number(row.return_count) || 0,
            qty_total: Number(row.qty_total) || 0,
            gross_total: Number(row.gross_total) || 0,
            net_total: Number(row.net_total) || 0,
            vat_total: Number(row.vat_total) || 0,
            sales_total: Number(row.sales_total) || 0
          };
          return;
        }
        cur.invoice_count += Number(row.invoice_count) || 0;
        cur.return_count += Number(row.return_count) || 0;
        cur.qty_total += Number(row.qty_total) || 0;
        cur.gross_total += Number(row.gross_total) || 0;
        cur.net_total += Number(row.net_total) || 0;
        cur.vat_total += Number(row.vat_total) || 0;
        cur.sales_total += Number(row.sales_total) || 0;
      });
    }
    ingest(basePanel || {});
    ingest(nextPanel || {});
    var rows = Object.keys(map).map(function (k) {
      var r = map[k];
      r.avg_basket = r.invoice_count ? r.sales_total / r.invoice_count : 0;
      r.qty_total_display = fmtMoneyNum(r.qty_total);
      r.net_total_display = fmtMoneyNum(r.net_total);
      r.vat_total_display = fmtMoneyNum(r.vat_total);
      r.sales_total_display = fmtMoneyNum(r.sales_total);
      r.gross_total_display = fmtMoneyNum(r.gross_total);
      r.avg_basket_display = fmtMoneyNum(r.avg_basket);
      r.invoice_count_display = String(r.invoice_count);
      r.return_count_display = String(r.return_count);
      return r;
    });
    rows.sort(function (a, b) {
      return (b.sales_total - a.sales_total) || String(a.group_name || "").localeCompare(String(b.group_name || ""), "ar");
    });
    var gInv = 0, gQty = 0, gSales = 0, gGross = 0, gNet = 0, gVat = 0;
    rows.forEach(function (r) {
      gInv += r.invoice_count;
      gQty += r.qty_total;
      gSales += r.sales_total;
      gGross += r.gross_total;
      gNet += r.net_total;
      gVat += r.vat_total;
    });
    // فواتير فريدة من رأس الفاتورة (من السيرفر) — لا نجمع صفوف المجموعات
    var uniqInv =
      (Number(basePanel.grand_invoices) || 0) + (Number(nextPanel.grand_invoices) || 0);
    var footerInv = byBranch ? gInv : uniqInv;
    var grandAvg = footerInv ? fmtMoneyNum(gSales / footerInv) : "0.00";
    return {
      rows: rows,
      groups: nextPanel.groups || basePanel.groups || [],
      selected_group: nextPanel.selected_group || basePanel.selected_group || "",
      selected_group_name: nextPanel.selected_group_name || basePanel.selected_group_name || "",
      selected_branch: nextPanel.selected_branch || basePanel.selected_branch || "",
      by_branch: byBranch,
      grand_invoices: footerInv,
      grand_invoices_display: String(footerInv),
      grand_qty_display: fmtMoneyNum(gQty),
      grand_sales: fmtMoneyNum(gSales),
      grand_gross: fmtMoneyNum(gGross),
      grand_net: fmtMoneyNum(gNet),
      grand_vat: fmtMoneyNum(gVat),
      grand_avg_basket: grandAvg,
      fast_mode: !!(basePanel.fast_mode || nextPanel.fast_mode),
      loading: false
    };
  }

  function groupsChunkUrl(baseUrl, chunk) {
    try {
      var u = new URL(baseUrl, window.location.origin);
      u.searchParams.set("date_from", chunk.date_from);
      u.searchParams.set("date_to", chunk.date_to);
      // دائماً صافي بعد المرتجعات — لا fast=1
      u.searchParams.delete("fast");
      return u.pathname + u.search;
    } catch (e) {
      var join = String(baseUrl || "").indexOf("?") >= 0 ? "&" : "?";
      return (
        String(baseUrl || "") +
        join +
        "date_from=" + encodeURIComponent(chunk.date_from) +
        "&date_to=" + encodeURIComponent(chunk.date_to)
      );
    }
  }

  function loadGroupsProgressive(baseUrl, dateFrom, dateTo) {
    var chunks = monthChunksNewestFirst(dateFrom, dateTo);
    if (!chunks.length) {
      return fetch(baseUrl, { credentials: "same-origin" })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (!data.ok) throw new Error(data.error || "فشل التحميل");
          renderGroups(data.panel || {});
        });
    }
    if (chunks.length === 1) {
      return fetch(groupsChunkUrl(baseUrl, chunks[0]), { credentials: "same-origin" })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (!data.ok) throw new Error(data.error || "فشل التحميل");
          renderGroups(data.panel || {});
        });
    }

    var merged = {
      rows: [],
      fast_mode: false,
      by_branch: false,
      grand_invoices: 0,
      grand_invoices_display: "0",
      grand_qty_display: "0.00",
      grand_sales: "0.00",
      grand_gross: "0.00",
      grand_net: "0.00",
      grand_vat: "0.00",
      grand_avg_basket: "0.00"
    };
    var idx = 0;
    var loading = document.getElementById("groups-loading");

    function step() {
      if (idx >= chunks.length) {
        merged.progressive_done = true;
        merged.progressive_pill = "اكتمل";
        renderGroups(merged);
        clearDashLoadProgress(loading);
        return Promise.resolve();
      }
      var chunk = chunks[idx];
      var n = idx + 1;
      var total = chunks.length;
      setDashLoadProgress(
        loading,
        Math.max(0, n - 1),
        total,
        "جاري تحميل المجموعات… شهر " + n + " من " + total +
          " (" + chunk.date_from.slice(0, 7) + ") — الأحدث أولاً"
      );
      return fetch(groupsChunkUrl(baseUrl, chunk), { credentials: "same-origin" })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (!data.ok) throw new Error(data.error || "فشل التحميل");
          merged = mergeGroupPanels(merged, data.panel || {});
          merged.progressive_done = n < total ? false : true;
          merged.progressive_pill = n + "/" + total;
          renderGroups(merged);
          if (n < total) {
            setDashLoadProgress(
              loading,
              n,
              total,
              "تم " + n + " من " + total + " — يُكمَّل الباقي…"
            );
          } else {
            clearDashLoadProgress(loading);
          }
          idx += 1;
          return step();
        });
    }
    return step();
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
        moneyHtml(b.sales_total_display) + " · " + esc(b.invoice_count_display) + " مرتجع" +
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
        '<span class="branch-chart-amt mono">' + moneyHtml(amt) + "</span>" +
        '<span class="branch-chart-inv mono">' + esc(item.qty_total_display) + " كمية مرتجعة</span>" +
        "</span></div>";
    });
    chartBoard.innerHTML = chartHtml;
  }

  function renderItems(items, keepProgress) {
    var loading = document.getElementById("items-loading");
    var board = document.getElementById("items-board");
    var pill = document.getElementById("items-pill");
    var list = items || [];

    if (loading && !keepProgress) {
      clearDashLoadProgress(loading);
    }
    if (pill) {
      var pillText = "أعلى " + (list.length || 20);
      if (keepProgress) pillText += " · جاري…";
      pill.textContent = pillText;
    }
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
        '<span class="seller-meta">' +
        '<span class="seller-meta-inv mono">' + esc(item.qty_total_display) + " كمية</span>" +
        '<span class="seller-meta-code mono">' + esc(item.invoice_count) + " سطر</span>" +
        "</span>" +
        '<span class="seller-track" aria-hidden="true"><span class="seller-fill"></span></span>' +
        "</span>" +
        '<span class="seller-side">' +
        moneyHtml(item.sales_total_display, "seller-amt mono money") +
        '<span class="seller-pct mono">' + esc(item.share_pct) + "%</span>" +
        "</span></div>";
    });
    board.innerHTML = html;
  }

  function finalizeTopItems(map, limit) {
    var list = Object.keys(map).map(function (k) { return map[k]; });
    list.sort(function (a, b) {
      return (b.sales_total - a.sales_total) || (b.qty_total - a.qty_total);
    });
    list = list.slice(0, Math.max(1, limit || 20));
    var peak = list.length ? list[0].sales_total : 0;
    list.forEach(function (row) {
      row.share_pct = peak ? Math.round((row.sales_total / peak) * 1000) / 10 : 0;
      row.sales_total_display = fmtMoneyNum(row.sales_total);
      row.qty_total_display = fmtMoneyNum(row.qty_total);
    });
    return list;
  }

  function mergeItemMaps(map, items) {
    (items || []).forEach(function (item) {
      var code = String(item.item_code || "").trim();
      if (!code) return;
      var cur = map[code];
      if (!cur) {
        map[code] = {
          item_code: code,
          item_name: item.item_name || code,
          invoice_count: Number(item.invoice_count) || 0,
          qty_total: Number(item.qty_total) || 0,
          sales_total: Number(item.sales_total) || 0,
          net_total: Number(item.net_total) || 0,
          vat_total: Number(item.vat_total) || 0
        };
        return;
      }
      cur.invoice_count += Number(item.invoice_count) || 0;
      cur.qty_total += Number(item.qty_total) || 0;
      cur.sales_total += Number(item.sales_total) || 0;
      cur.net_total += Number(item.net_total) || 0;
      cur.vat_total += Number(item.vat_total) || 0;
      if (item.item_name && item.item_name !== code) cur.item_name = item.item_name;
    });
    return map;
  }

  function loadItemsProgressive(baseUrl, dateFrom, dateTo) {
    var chunks = monthChunksNewestFirst(dateFrom, dateTo);
    var limit = 20;
    if (!chunks.length) {
      return fetch(baseUrl, { credentials: "same-origin" })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (!data.ok) throw new Error(data.error || "فشل التحميل");
          renderItems(data.items || []);
        });
    }
    var map = {};
    var idx = 0;
    var loading = document.getElementById("items-loading");
    function step() {
      if (idx >= chunks.length) {
        renderItems(finalizeTopItems(map, limit));
        clearDashLoadProgress(loading);
        return Promise.resolve();
      }
      var chunk = chunks[idx];
      var n = idx + 1;
      var total = chunks.length;
      setDashLoadProgress(
        loading,
        Math.max(0, n - 1),
        total,
        "جاري تحميل الأصناف… شهر " + n + " من " + total +
          " (" + chunk.date_from.slice(0, 7) + ")"
      );
      var url = groupsChunkUrl(baseUrl, chunk);
      if (url.indexOf("limit=") < 0) {
        url += (url.indexOf("?") >= 0 ? "&" : "?") + "limit=40";
      }
      return fetch(url, { credentials: "same-origin" })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (!data.ok) throw new Error(data.error || "فشل التحميل");
          mergeItemMaps(map, data.items || []);
          renderItems(finalizeTopItems(map, limit), n < total);
          if (n < total) {
            setDashLoadProgress(
              loading,
              n,
              total,
              "تم " + n + " من " + total + " — يُكمَّل الباقي…"
            );
          } else {
            clearDashLoadProgress(loading);
          }
          idx += 1;
          return step();
        });
    }
    return step();
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
        '<span class="seller-meta">' +
        '<span class="seller-meta-inv mono">' + esc(u.invoice_count) + " فاتورة</span>" +
        '<span class="seller-meta-code mono">#' + esc(u.user_code) + "</span>" +
        "</span>" +
        '<span class="seller-track" aria-hidden="true"><span class="seller-fill"></span></span>' +
        "</span>" +
        '<span class="seller-side">' +
        moneyHtml(u.sales_total_display, "seller-amt mono money") +
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
    var box = document.getElementById("dash-top-items");
    var loading = document.getElementById("items-loading");
    var board = document.getElementById("items-board");
    if (loading) {
      loading.hidden = false;
      loading.textContent = "جاري تحميل الأصناف…";
    }
    if (board) board.hidden = true;
    var progressive = box && box.dataset.itemsProgressive === "1";
    var dFrom = (box && box.dataset.dateFrom) || "";
    var dTo = (box && box.dataset.dateTo) || "";
    var p = progressive && dFrom && dTo
      ? loadItemsProgressive(url, dFrom, dTo)
      : fetch(url, { credentials: "same-origin" })
          .then(function (r) { return r.json(); })
          .then(function (data) {
            if (!data.ok) throw new Error(data.error || "فشل التحميل");
            renderItems(data.items || []);
          });
    p.catch(function (err) {
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
    var groupsUrl = groupsBox && groupsBox.dataset.groupsUrl;
    var panelsUrl =
      (groupsBox && groupsBox.dataset.panelsUrl) ||
      (itemsBox && itemsBox.dataset.panelsUrl) ||
      "";
    var itemsUrl = itemsBox && itemsBox.dataset.itemsUrl;

    // الفترات الطويلة: مجموعات شهراً بشهر (الأحدث أولاً) ثم الأصناف/المرتجعات
    if (groupsUrl) {
      var progressive = groupsBox && groupsBox.dataset.groupsProgressive === "1";
      var dFrom = (groupsBox && groupsBox.dataset.dateFrom) || "";
      var dTo = (groupsBox && groupsBox.dataset.dateTo) || "";
      var groupsPromise = progressive && dFrom && dTo
        ? loadGroupsProgressive(groupsUrl, dFrom, dTo)
        : fetch(groupsUrl, { credentials: "same-origin" })
            .then(function (r) { return r.json(); })
            .then(function (data) {
              if (!data.ok) throw new Error(data.error || "فشل التحميل");
              renderGroups(data.panel || {});
            });
      groupsPromise.catch(failGroups);

      if (itemsUrl) {
        var itemsProgressive = itemsBox && itemsBox.dataset.itemsProgressive === "1";
        var iFrom = (itemsBox && itemsBox.dataset.dateFrom) || "";
        var iTo = (itemsBox && itemsBox.dataset.dateTo) || "";
        var itemsPromise = itemsProgressive && iFrom && iTo
          ? loadItemsProgressive(itemsUrl, iFrom, iTo)
          : fetch(itemsUrl, { credentials: "same-origin" })
              .then(function (r) { return r.json(); })
              .then(function (data) {
                if (!data.ok) throw new Error(data.error || "فشل التحميل");
                renderItems(data.items || []);
              });
        itemsPromise.catch(failItems);
      }

      setTimeout(function () {
        // طلب واحد يحدّث رسم الفروع + أصناف المرتجعات (بدون تكرار)
        loadBranchChart();
      }, 120);
      return true;
    }

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
      '<tr class="sales-grand-row">' +
      '<td colspan="2">الإجمالي</td>' +
      '<td class="mono sales-amt sales-amt-net">' + moneyHtml(totals.sales_net_display) + "</td>" +
      '<td class="mono sales-amt sales-amt-gross margin-cost">' + moneyHtml(totals.cost_total_display) + "</td>" +
      '<td class="mono sales-amt ' + tone + '">' + moneyHtml(totals.profit_display) + "</td>" +
      '<td class="mono ' + tone + '">' + esc(totals.margin_pct_display) + "</td>" +
      "</tr>";
  }

  function renderMarginBranches(rows, keepProgress) {
    var loading = document.getElementById("margin-branches-loading");
    var block = document.getElementById("margin-branches-block");
    var wrap = document.getElementById("margin-branches-wrap");
    var empty = document.getElementById("margin-branches-empty");
    var body = document.getElementById("margin-branches-body");
    var pill = document.getElementById("margin-branches-pill");
    var list = rows || [];
    if (loading && !keepProgress) clearDashLoadProgress(loading);
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
        "<td title=\"" + esc((row.branch_name || "") + " " + (row.branch_code || "")) + "\">" + esc(row.branch_name || row.branch_code || "") + "</td>" +
        '<td class="mono sales-amt">' + moneyHtml(row.sales_net_display || "0.00") + "</td>" +
        '<td class="mono margin-cost">' + moneyHtml(row.cost_total_display || "0.00") + "</td>" +
        '<td class="mono ' + tone + '">' + moneyHtml(row.profit_display || "0.00") + "</td>" +
        '<td class="mono ' + tone + '">' + esc(row.margin_pct_display || "—") + "</td>" +
        "</tr>"
      );
    }).join("");
    renderMarginTotals("margin-branches-totals", marginTotals(list));
  }

  function renderMarginGroups(rows, keepProgress) {
    var loading = document.getElementById("margin-groups-loading");
    var block = document.getElementById("margin-groups-block");
    var wrap = document.getElementById("margin-groups-wrap");
    var empty = document.getElementById("margin-groups-empty");
    var body = document.getElementById("margin-groups-body");
    var pill = document.getElementById("margin-groups-pill");
    var list = rows || [];
    if (loading && !keepProgress) clearDashLoadProgress(loading);
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
        "<td title=\"" + esc((row.group_name || "") + " " + (row.group_code || "")) + "\">" + esc(row.group_name || row.group_code || "") + "</td>" +
        '<td class="mono sales-amt">' + moneyHtml(row.sales_net_display || "0.00") + "</td>" +
        '<td class="mono margin-cost">' + moneyHtml(row.cost_total_display || "0.00") + "</td>" +
        '<td class="mono ' + tone + '">' + moneyHtml(row.profit_display || "0.00") + "</td>" +
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

  function formatMarginRows(rows, codeKey, nameKey) {
    return (rows || []).map(function (row) {
      var sales = Number(row.sales_net) || 0;
      var cost = Number(row.cost_total) || 0;
      var profit = Number(row.profit);
      if (!isFinite(profit)) profit = sales - cost;
      var pct = cost > 0 ? Math.round(((profit / cost) * 10000)) / 100 : null;
      var out = {
        qty_total: Number(row.qty_total) || 0,
        sales_net: sales,
        cost_total: cost,
        profit: profit,
        margin_pct: pct,
        sales_net_display: fmtMarginMoney(sales),
        cost_total_display: fmtMarginMoney(cost),
        profit_display: fmtMarginMoney(profit),
        margin_pct_display: pct == null ? "—" : fmtMarginMoney(pct) + "%",
        qty_total_display: fmtMarginMoney(Number(row.qty_total) || 0)
      };
      out[codeKey] = row[codeKey];
      out[nameKey] = row[nameKey];
      return out;
    }).sort(function (a, b) {
      if (a.margin_pct == null && b.margin_pct == null) return (b.profit - a.profit);
      if (a.margin_pct == null) return 1;
      if (b.margin_pct == null) return -1;
      return (b.margin_pct - a.margin_pct) || (b.profit - a.profit);
    });
  }

  function mergeMarginMaps(map, rows, codeKey, nameKey) {
    (rows || []).forEach(function (row) {
      var code = String(row[codeKey] || "").trim();
      if (!code) return;
      var cur = map[code];
      if (!cur) {
        map[code] = {
          qty_total: Number(row.qty_total) || 0,
          sales_net: Number(row.sales_net) || 0,
          cost_total: Number(row.cost_total) || 0
        };
        map[code][codeKey] = code;
        map[code][nameKey] = row[nameKey] || code;
        return;
      }
      cur.qty_total += Number(row.qty_total) || 0;
      cur.sales_net += Number(row.sales_net) || 0;
      cur.cost_total += Number(row.cost_total) || 0;
      if (row[nameKey]) cur[nameKey] = row[nameKey];
    });
    return map;
  }

  var marginsProgByUrl = {};

  function loadMarginsProgressive(baseUrl, dateFrom, dateTo) {
    var dedupeKey = String(baseUrl || "") + "|" + dateFrom + "|" + dateTo;
    if (marginsProgByUrl[dedupeKey]) return marginsProgByUrl[dedupeKey];

    var chunks = marginChunksNewestFirst(dateFrom, dateTo);
    var loadingBranches = document.getElementById("margin-branches-loading");
    var loadingGroups = document.getElementById("margin-groups-loading");
    if (!chunks.length) {
      var p0 = fetchJsonRetry(baseUrl).then(function (data) {
        if (!data.ok) throw new Error(data.error || "فشل التحميل");
        renderMarginBranches(data.branches || []);
        renderMarginGroups(data.groups || []);
      });
      marginsProgByUrl[dedupeKey] = p0.finally(function () { delete marginsProgByUrl[dedupeKey]; });
      return marginsProgByUrl[dedupeKey];
    }

    var brMap = {};
    var grMap = {};
    var total = chunks.length;
    var done = 0;
    var failed = 0;
    var nextIdx = 0;
    var CONCURRENCY = Math.min(2, total);

    function setLoading(doneN, totalN, label) {
      setDashLoadProgress(loadingBranches, doneN, totalN, label);
      setDashLoadProgress(loadingGroups, doneN, totalN, label);
    }

    function paint(stillLoading) {
      var brRows = formatMarginRows(
        Object.keys(brMap).map(function (k) { return brMap[k]; }),
        "branch_code",
        "branch_name"
      );
      var grRows = formatMarginRows(
        Object.keys(grMap).map(function (k) { return grMap[k]; }),
        "group_code",
        "group_name"
      );
      renderMarginBranches(brRows, stillLoading);
      renderMarginGroups(grRows, stillLoading);
    }

    function chunkLabel(chunk) {
      return chunk.date_from.slice(0, 7) +
        (chunk.date_from.slice(8) !== "01" || chunk.date_to.slice(8) < "28"
          ? (" " + chunk.date_from.slice(8) + "–" + chunk.date_to.slice(8))
          : "");
    }

    function worker() {
      if (nextIdx >= chunks.length) return Promise.resolve();
      var i = nextIdx;
      nextIdx += 1;
      var chunk = chunks[i];
      var n = i + 1;
      setLoading(
        done,
        total,
        "جاري تحميل الهامش… شريحة " + n + " من " + total +
          " (" + chunkLabel(chunk) + ") — الأحدث أولاً"
      );
      return fetchJsonRetry(groupsChunkUrl(baseUrl, chunk), { attempts: 3, timeoutMs: 150000 })
        .then(function (data) {
          if (!data.ok) throw new Error(data.error || "فشل التحميل");
          mergeMarginMaps(brMap, data.branches || [], "branch_code", "branch_name");
          mergeMarginMaps(grMap, data.groups || [], "group_code", "group_name");
        })
        .catch(function () {
          failed += 1;
        })
        .then(function () {
          done += 1;
          var still = done < total;
          paint(still);
          if (still) {
            var msg = failed
              ? ("تم " + (done - failed) + " من " + total + " — تعذّر " + failed + " — يُكمَّل…")
              : ("تم " + done + " من " + total + " — يُكمَّل الباقي…");
            setLoading(done, total, msg);
          }
          return worker();
        });
    }

    var workers = [];
    for (var w = 0; w < CONCURRENCY; w += 1) workers.push(worker());
    var p = Promise.all(workers).then(function () {
      paint(false);
      clearDashLoadProgress(loadingBranches);
      clearDashLoadProgress(loadingGroups);
      if (failed && !Object.keys(brMap).length && !Object.keys(grMap).length) {
        throw new Error("تعذّر تحميل شرائح الهامش (" + failed + "/" + total + ")");
      }
      if (failed && loadingBranches) {
        loadingBranches.hidden = false;
        loadingBranches.classList.remove("dash-load-progress");
        loadingBranches.textContent =
          "اكتمل جزئياً — تعذّر " + failed + " من " + total + " شرائح (صافي بعد المرتجعات).";
      }
    });
    marginsProgByUrl[dedupeKey] = p.finally(function () { delete marginsProgByUrl[dedupeKey]; });
    return marginsProgByUrl[dedupeKey];
  }

  function loadMarginBranches() {
    var box = document.getElementById("dash-margins");
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
    var progressive = box && box.dataset.marginsProgressive === "1";
    var dFrom = (box && box.dataset.dateFrom) || "";
    var dTo = (box && box.dataset.dateTo) || "";
    var p;
    if (progressive && dFrom && dTo) {
      p = loadMarginsProgressive(url, dFrom, dTo).then(function () {
        /* groups also filled */
      });
    } else {
      p = fetch(url, { credentials: "same-origin" })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (!data.ok) throw new Error(data.error || "فشل التحميل");
          renderMarginBranches(data.branches || []);
        });
    }
    p.catch(function (err) {
      if (loading) loading.textContent = "تعذّر تحميل هامش الفروع: " + (err.message || err);
    });
  }

  function loadMarginGroups() {
    var box = document.getElementById("dash-margins");
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
    var progressive = box && box.dataset.marginsProgressive === "1";
    var dFrom = (box && box.dataset.dateFrom) || "";
    var dTo = (box && box.dataset.dateTo) || "";
    // عند التحميل التصاعدي يُحدَّث الطرفان معاً من loadMargins / loadMarginBranches
    if (progressive && dFrom && dTo) {
      loadMarginsProgressive(url, dFrom, dTo).catch(function (err) {
        if (loading) loading.textContent = "تعذّر تحميل هامش المجموعات: " + (err.message || err);
      });
      return;
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
    var brSel = document.getElementById("margin-br-branch");
    var brGrp = document.getElementById("margin-br-group");
    var grSel = document.getElementById("margin-gr-branch");
    var grGrp = document.getElementById("margin-gr-group");
    var sameFilters =
      (!brSel || !grSel || brSel.value === grSel.value) &&
      (!brGrp || !grGrp || brGrp.value === grGrp.value);
    var progressive = box.dataset.marginsProgressive === "1";
    var dFrom = box.dataset.dateFrom || "";
    var dTo = box.dataset.dateTo || "";
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
      var p = progressive && dFrom && dTo
        ? loadMarginsProgressive(url, dFrom, dTo)
        : fetch(url, { credentials: "same-origin" })
            .then(function (r) { return r.json(); })
            .then(function (data) {
              if (!data.ok) throw new Error(data.error || "فشل التحميل");
              renderMarginBranches(data.branches || []);
              renderMarginGroups(data.groups || []);
            });
      p.catch(function (err) {
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

  function applyKpiHighlights(h) {
    if (!h) return;
    var amtName = document.getElementById("kpi-item-amt-name");
    var amtHint = document.getElementById("kpi-item-amt-hint");
    var qtyName = document.getElementById("kpi-item-qty-name");
    var qtyHint = document.getElementById("kpi-item-qty-hint");
    var retName = document.getElementById("kpi-item-ret-name");
    var retHint = document.getElementById("kpi-item-ret-hint");
    if (amtName) {
      amtName.textContent = h.top_amount_name || "—";
      amtName.title = h.top_amount_code || "";
    }
    if (amtHint) {
      amtHint.innerHTML = moneyHtml(h.top_amount_value || "0.00") + " مبلغ";
    }
    if (qtyName) {
      qtyName.textContent = h.top_qty_name || "—";
      qtyName.title = h.top_qty_code || "";
    }
    if (qtyHint) {
      qtyHint.textContent = (h.top_qty_value || "0") + " كمية";
    }
    if (retName) {
      retName.textContent = h.top_return_name || "—";
      retName.title = h.top_return_code || "";
    }
    if (retHint) {
      var sysLabel = "";
      var tab = document.querySelector(".dash-tab.is-active");
      // احتفظ بنص النظام إن وُجد في التلميح الأصلي
      var old = retHint.textContent || "";
      var parts = old.split("·");
      if (parts.length > 1) sysLabel = " ·" + parts.slice(1).join("·");
      retHint.innerHTML =
        moneyHtml(h.top_return_value || "0.00") + " مبلغ مرتجع" + esc(sysLabel);
    }
  }

  function loadKpiHighlights() {
    var box = document.querySelector(".dash-kpi[data-highlights-url]");
    if (!box || !box.dataset.highlightsUrl) return;
    fetch(box.dataset.highlightsUrl, { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data.ok) throw new Error(data.error || "فشل التحميل");
        applyKpiHighlights(data.highlights || {});
      })
      .catch(function () {
        applyKpiHighlights({
          top_amount_name: "—",
          top_qty_name: "—",
          top_return_name: "—"
        });
      });
  }

  var seedEl = document.getElementById("chart-branches-seed");
  var seedEmpty = true;
  if (seedEl) {
    try {
      var seeded = JSON.parse(seedEl.textContent || "[]");
      seedEmpty = !seeded || !seeded.length;
      renderChartBranches(seeded);
    } catch (e) { /* keep server HTML if any */ }
  }

  loadKpiHighlights();
  // البائعون + الهامش بعد أول رسم حتى لا يزاحموا أوراكل مع المجموعات
  setTimeout(function () {
    var sellersBoard = document.getElementById("sellers-board");
    var hasSellerCards = sellersBoard && sellersBoard.querySelector(".seller-card");
    if (!hasSellerCards) loadSideSellers();
  }, 250);
  setTimeout(function () { loadMargins(); }, 450);
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
    setTimeout(function () { loadBranchChart(); }, 150);
  } else if (seedEmpty) {
    // loadPanels يحجز loadBranchChart — لا تكرار هنا
  }
})();
