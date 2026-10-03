(function () {
  "use strict";

  var DAY_MS = 86400000;
  var started = 0;
  var PAGE_ITEMS = 20;
  var groups = [];
  var meta = null;
  var current = 1;

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function daysAgo(iso) {
    var t = Date.parse(iso + "T00:00:00");
    if (isNaN(t)) return null;
    var today = new Date();
    today.setHours(0, 0, 0, 0);
    return Math.max(0, Math.round((today.getTime() - t) / DAY_MS));
  }

  function ageText(n) {
    if (n === 0) return "اليوم";
    if (n === 1) return "أمس";
    if (n < 30) return "قبل " + n + " يوم";
    if (n < 365) return "قبل " + Math.round(n / 30) + " شهر";
    return "قبل " + (n / 365).toFixed(1) + " سنة";
  }

  function dateCell(iso) {
    if (!iso) return '<span class="lm-none" title="لا حركة ضمن الفترة">—</span>';
    var n = daysAgo(iso);
    var tone = n == null ? "" : n <= 30 ? " is-fresh" : n <= 180 ? " is-mid" : " is-old";
    return (
      '<span class="lm-day' +
      tone +
      '">' +
      esc(iso) +
      "</span>" +
      (n == null ? "" : '<small class="lm-age">' + ageText(n) + "</small>")
    );
  }

  function fmtQty(n) {
    return Number(n).toLocaleString("en-US", { maximumFractionDigits: 2 });
  }

  function qtyHtml(rec, code) {
    if (!rec || !rec.stock || !rec.stock.length) {
      return '<span class="lm-qty-val is-zero-val">0</span>';
    }
    return (
      '<span class="lm-qty-line"><span class="lm-qty-val">' + fmtQty(rec.stock_total) + "</span>" +
      (rec.stock_unit ? '<small class="lm-unit">' + esc(rec.stock_unit) + "</small>" : "") +
      '<button type="button" class="lm-more" data-code="' + esc(code) +
      '" aria-haspopup="true" title="تفصيل الكمية حسب المخزن">' + rec.stock.length + " ▾</button></span>"
    );
  }

  var pop = null;
  var popFor = null;

  function closePop() {
    if (pop) pop.hidden = true;
    popFor = null;
  }

  function openPop(btn) {
    var code = btn.getAttribute("data-code");
    var rec = purchaseCache[code];
    if (!rec || !rec.stock) return;
    if (!pop) {
      pop = document.createElement("div");
      pop.className = "lm-pop";
      pop.setAttribute("role", "dialog");
      document.body.appendChild(pop);
    }
    if (popFor === btn && !pop.hidden) {
      closePop();
      return;
    }
    var html = '<div class="lm-pop-title">الكمية حسب المخزن</div><ul>';
    rec.stock.forEach(function (s) {
      html +=
        "<li><span>" + esc(s.wh_name) + '</span><b class="mono">' + fmtQty(s.qty) +
        (s.unit ? " " + esc(s.unit) : "") + "</b></li>";
    });
    html +=
      '<li class="lm-pop-total"><span>الإجمالي</span><b class="mono">' +
      fmtQty(rec.stock_total) + (rec.stock_unit ? " " + esc(rec.stock_unit) : "") +
      "</b></li></ul>";
    pop.innerHTML = html;
    pop.hidden = false;
    var r = btn.getBoundingClientRect();
    var w = pop.offsetWidth;
    var h = pop.offsetHeight;
    var left = Math.min(Math.max(8, r.left), window.innerWidth - w - 8);
    var top = r.bottom + 6;
    if (top + h > window.innerHeight - 8) top = Math.max(8, r.top - h - 6);
    pop.style.left = left + "px";
    pop.style.top = top + "px";
    popFor = btn;
  }

  function setStatus(kind, text) {
    var el = document.getElementById("lm-status");
    if (!el) return;
    el.className = "sales-groups-status is-" + kind;
    el.textContent = text;
  }

  function setBody(html) {
    var body = document.getElementById("lm-body");
    if (body) body.innerHTML = html;
  }

  function fail(msg) {
    setStatus("error", "غير مكتمل");
    setBody('<tr><td colspan="9" class="sales-empty">' + esc(msg) + "</td></tr>");
    var pill = document.getElementById("lm-pill");
    if (pill) pill.textContent = "!";
  }

  function groupRows(rows) {
    var out = [];
    var last = null;
    rows.forEach(function (r) {
      if (!last || last.code !== r.item_code) {
        last = { code: r.item_code, rows: [] };
        out.push(last);
      }
      last.rows.push(r);
    });
    return out;
  }

  function rowHtml(r, n, first) {
    return (
      "<tr>" +
      '<td class="mono">' + (first ? n : "") + "</td>" +
      '<td class="pr-item" title="' + esc(r.name) + '">' +
      esc(r.name || "—") +
      ' <small class="mono">#' + esc(r.item_code) + "</small></td>" +
      '<td class="mono lm-barcode">' + esc(r.barcode || "—") + "</td>" +
      "<td>" + esc(r.unit || "—") + "</td>" +
      '<td class="pr-group" title="' + esc(r.group_name) + '">' + esc(r.group_name || "—") + "</td>" +
      '<td class="mono lm-date lm-purchase" data-code="' + esc(r.item_code) + '"><span class="lm-pending">…</span></td>' +
      '<td class="lm-wh lm-purchase-wh" data-code="' + esc(r.item_code) + '"><span class="lm-pending">…</span></td>' +
      '<td class="mono lm-date">' + dateCell(r.sale) + "</td>" +
      '<td class="lm-qty lm-stock" data-code="' + esc(r.item_code) + '"><span class="lm-pending">…</span></td>' +
      "</tr>"
    );
  }

  function pageList(total, cur) {
    var pages = [];
    var add = function (p) { if (pages.indexOf(p) < 0) pages.push(p); };
    add(1);
    for (var p = cur - 2; p <= cur + 2; p++) if (p > 1 && p < total) add(p);
    if (total > 1) add(total);
    pages.sort(function (a, b) { return a - b; });
    var out = [];
    pages.forEach(function (p, i) {
      if (i && p - pages[i - 1] > 1) out.push("…");
      out.push(p);
    });
    return out;
  }

  function renderPager(totalPages) {
    var pager = document.getElementById("lm-pager");
    if (!pager) return;
    pager.hidden = totalPages <= 1;
    if (totalPages <= 1) return;
    var html =
      '<button type="button" class="lm-pg" data-page="' + (current - 1) + '"' +
      (current <= 1 ? " disabled" : "") + ">السابق</button>";
    pageList(totalPages, current).forEach(function (p) {
      html += p === "…"
        ? '<span class="lm-pg-gap">…</span>'
        : '<button type="button" class="lm-pg' + (p === current ? " is-current" : "") +
          '" data-page="' + p + '">' + p + "</button>";
    });
    html += '<button type="button" class="lm-pg" data-page="' + (current + 1) + '"' +
      (current >= totalPages ? " disabled" : "") + ">التالي</button>";
    pager.innerHTML = html;
  }

  function showPage(p) {
    var totalPages = Math.max(1, Math.ceil(groups.length / PAGE_ITEMS));
    current = Math.min(Math.max(1, p), totalPages);
    var slice = groups.slice((current - 1) * PAGE_ITEMS, current * PAGE_ITEMS);
    var html = "";
    var n = (current - 1) * PAGE_ITEMS;
    slice.forEach(function (g) {
      n += 1;
      g.rows.forEach(function (r, i) { html += rowHtml(r, n, i === 0); });
    });
    setBody(
      html ||
        '<tr><td colspan="9" class="sales-empty">لا أصناف لها حركة ضمن الفلتر والفترة المحددة</td></tr>'
    );
    closePop();
    renderPager(totalPages);
    loadPurchases(slice);
    var pill = document.getElementById("lm-pill");
    if (pill && meta) {
      pill.textContent = groups.length
        ? "صفحة " + current + " / " + totalPages + " · " + meta.total + " صنف"
        : "0 صنف";
    }
  }

  var purchaseCache = {};

  function fillPurchases(map) {
    document.querySelectorAll("#lm-body td.lm-purchase").forEach(function (td) {
      var code = td.getAttribute("data-code");
      var rec = map[code] || null;
      var ok = /^\d+$/.test(code || "");
      td.innerHTML = ok ? dateCell(rec ? rec.date : "") : '<span class="lm-none">—</span>';
      var stTd = td.parentNode.querySelector("td.lm-stock");
      if (stTd) stTd.innerHTML = ok ? qtyHtml(rec, code) : '<span class="lm-none">—</span>';
      var whTd = td.parentNode.querySelector("td.lm-purchase-wh");
      if (whTd) {
        whTd.innerHTML =
          ok && rec && rec.wh
            ? '<span title="' + esc(rec.wh_name || rec.wh) + '">' + esc(rec.wh_name || rec.wh) + "</span>"
            : '<span class="lm-none">—</span>';
      }
    });
  }

  function loadPurchases(slice) {
    var table = document.getElementById("lm-table");
    var codes = [];
    slice.forEach(function (g) {
      if (/^\d+$/.test(g.code) && codes.indexOf(g.code) < 0) codes.push(g.code);
    });
    var need = codes.filter(function (c) { return !(c in purchaseCache); });
    if (!need.length) {
      fillPurchases(purchaseCache);
      return;
    }
    var qs = new URLSearchParams({
      codes: need.join(","),
      branch: table.getAttribute("data-branch") || "",
    });
    var reqPage = current;
    fetch(table.getAttribute("data-purchases-api") + "?" + qs.toString(), {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
      cache: "no-store",
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data || data.ok === false) throw new Error("fail");
        need.forEach(function (c) { purchaseCache[c] = (data.rows || {})[c] || null; });
        if (reqPage === current) fillPurchases(purchaseCache);
      })
      .catch(function () {
        document.querySelectorAll("#lm-body .lm-pending").forEach(function (el) {
          el.textContent = "—";
        });
      });
  }

  function render(data) {
    var table = document.getElementById("lm-table");
    var sub = document.getElementById("lm-sub");
    meta = data;
    purchaseCache = {};
    groups = groupRows(data.rows || []);
    var secs = Math.max(1, Math.round((Date.now() - started) / 1000));
    setStatus("ready", "مكتمل ✓ " + secs + "ث");
    if (sub) {
      sub.textContent =
        table.getAttribute("data-date-from") +
        " → " +
        table.getAttribute("data-date-to") +
        " · مرتبة بالأحدث حركة" +
        (data.shown < data.total
          ? " · عُرض أحدث " + data.shown + " من " + data.total + " — ضيّق الفلتر لعرض الباقي"
          : "");
    }
    showPage(1);
  }

  function load() {
    var table = document.getElementById("lm-table");
    if (!table) return;
    var qs = new URLSearchParams({
      date_from: table.getAttribute("data-date-from") || "",
      date_to: table.getAttribute("data-date-to") || "",
      branch: table.getAttribute("data-branch") || "",
      group: table.getAttribute("data-group") || "",
      q: table.getAttribute("data-q") || "",
    });
    started = Date.now();
    setStatus("loading", "جاري التحميل…");
    setBody('<tr><td colspan="9" class="sales-empty sales-ov-loading">جاري تحميل الأصناف…</td></tr>');
    fetch(table.getAttribute("data-api") + "?" + qs.toString(), {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
      cache: "no-store",
    })
      .then(function (r) {
        return r.json().then(function (data) {
          if (!r.ok || !data || data.ok === false) {
            throw new Error((data && data.error) || "HTTP " + r.status);
          }
          return data;
        });
      })
      .then(render)
      .catch(function (err) {
        fail((err && err.message) || "تعذّر تحميل البيانات");
      });
  }

  function init() {
    var pager = document.getElementById("lm-pager");
    if (pager) {
      pager.addEventListener("click", function (ev) {
        var btn = ev.target.closest(".lm-pg[data-page]");
        if (!btn || btn.disabled) return;
        showPage(parseInt(btn.getAttribute("data-page"), 10) || 1);
        var panel = document.getElementById("lm-table");
        if (panel && panel.scrollIntoView) panel.scrollIntoView({ block: "start" });
      });
    }
    document.addEventListener("click", function (ev) {
      var btn = ev.target.closest(".lm-more");
      if (btn) {
        ev.stopPropagation();
        openPop(btn);
        return;
      }
      if (pop && !ev.target.closest(".lm-pop")) closePop();
    });
    document.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape") closePop();
    });
    window.addEventListener("scroll", closePop, { passive: true });
    window.addEventListener("resize", closePop);
    load();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
