(function () {
  "use strict";

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function moneyHtml(v) {
    return (
      '<span class="money">' +
      esc(v) +
      ' <span class="sar-symbol" aria-hidden="true">ر.س</span></span>'
    );
  }

  function formatDuration(ms) {
    var totalSec = Math.max(0, Math.round((Number(ms) || 0) / 1000));
    var mins = Math.floor(totalSec / 60);
    var secs = totalSec % 60;
    if (mins <= 0) return secs + "ث";
    if (secs <= 0) return mins + "د";
    return mins + "د " + secs + "ث";
  }

  function readUrl() {
    var seed = document.getElementById("sales-items-url");
    if (!seed) return "";
    try {
      return String(JSON.parse(seed.textContent || '""') || "");
    } catch (e) {
      return "";
    }
  }

  function setLoading(msg) {
    var body = document.getElementById("sales-items-body");
    var sub = document.getElementById("sales-items-sub");
    var pill = document.getElementById("sales-items-pill");
    if (body) {
      body.innerHTML =
        '<tr><td colspan="6" class="sales-empty sales-ov-loading">' +
        esc(msg) +
        "</td></tr>";
    }
    if (sub) sub.textContent = "نقاط البيع · " + msg;
    if (pill) pill.textContent = "…";
  }

  function fail(msg, elapsedMs) {
    var body = document.getElementById("sales-items-body");
    var sub = document.getElementById("sales-items-sub");
    var pill = document.getElementById("sales-items-pill");
    if (body) {
      body.innerHTML =
        '<tr><td colspan="6" class="sales-empty">' + esc(msg) + "</td></tr>";
    }
    if (pill) pill.textContent = "!";
    if (sub) {
      sub.textContent =
        "نقاط البيع · فشل التحميل · بعد " + formatDuration(elapsedMs);
    }
  }

  function render(data, elapsedMs) {
    var body = document.getElementById("sales-items-body");
    var foot = document.getElementById("sales-items-foot");
    var sub = document.getElementById("sales-items-sub");
    var pill = document.getElementById("sales-items-pill");
    var totInv = document.getElementById("sales-items-tot-inv");
    var totQty = document.getElementById("sales-items-tot-qty");
    var totSales = document.getElementById("sales-items-tot-sales");
    var took = formatDuration(elapsedMs);

    if (!data || !data.ok || !data.items) {
      fail((data && data.error) || "تعذّر تحميل أصناف الإرجاع", elapsedMs);
      return;
    }

    var rows = data.items.rows || [];
    var totals = data.items.totals || {};

    if (!rows.length) {
      if (body) {
        body.innerHTML =
          '<tr><td colspan="6" class="sales-empty">لا مرتجعات أصناف في الفترة.</td></tr>';
      }
      if (foot) foot.hidden = true;
      if (pill) pill.textContent = "0";
      if (sub) sub.textContent = "نقاط البيع · لا بيانات · خلال " + took;
      return;
    }

    var html = "";
    rows.forEach(function (row, i) {
      html +=
        "<tr>" +
        '<td class="mono">' +
        (row.rank || i + 1) +
        "</td>" +
        '<td title="' +
        esc(row.item_code) +
        '">' +
        esc(row.item_name) +
        "</td>" +
        '<td class="mono">' +
        esc(row.return_count_display || row.invoice_count_display) +
        "</td>" +
        '<td class="mono">' +
        esc(row.qty_display) +
        "</td>" +
        '<td class="mono sales-amt">' +
        moneyHtml(row.return_total_display || row.sales_total_display) +
        "</td>" +
        '<td class="mono">' +
        esc(row.share_display) +
        "</td>" +
        "</tr>";
    });
    if (body) body.innerHTML = html;
    if (totInv) {
      totInv.textContent =
        totals.return_count_display || totals.invoice_count_display || "0";
    }
    if (totQty) totQty.textContent = totals.qty_display || "0";
    if (totSales) {
      totSales.innerHTML = moneyHtml(
        totals.return_total_display || totals.sales_total_display || "0.00"
      );
    }
    if (foot) foot.hidden = false;
    if (pill) pill.textContent = String(rows.length);
    if (sub) {
      sub.textContent =
        "نقاط البيع · " +
        rows.length +
        " صنف · إجمالي مرتجع " +
        (totals.return_total_display || totals.sales_total_display || "0.00") +
        " · خلال " +
        took;
    }
  }

  function init() {
    var url = readUrl();
    if (!url) return;

    var started = Date.now();
    setLoading("جاري التحميل… 0ث");
    var tick = setInterval(function () {
      setLoading("جاري التحميل… " + formatDuration(Date.now() - started));
    }, 1000);

    fetch(url, {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
      cache: "no-store",
    })
      .then(function (r) {
        var ct = (r.headers.get("content-type") || "").toLowerCase();
        if (!r.ok) throw new Error("HTTP " + r.status);
        if (ct.indexOf("application/json") === -1) {
          throw new Error("استجابة غير JSON");
        }
        return r.json();
      })
      .then(function (data) {
        clearInterval(tick);
        render(data, Date.now() - started);
      })
      .catch(function (err) {
        clearInterval(tick);
        fail(
          (err && err.message) || "تعذّر تحميل أصناف الإرجاع",
          Date.now() - started
        );
      });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
