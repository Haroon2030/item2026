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
    var seed = document.getElementById("sales-groups-url");
    if (seed) {
      try {
        return String(JSON.parse(seed.textContent || '""') || "");
      } catch (e) {
        /* fall through */
      }
    }
    var panel = document.getElementById("sales-groups-panel");
    if (!panel) return "";
    var raw = panel.getAttribute("data-groups-url") || "";
    // فك &#38; / &amp; إن بقيت مشفّرة في الخاصية
    var ta = document.createElement("textarea");
    ta.innerHTML = raw;
    return String(ta.value || "").trim();
  }

  function setLoading(body, sub, pill, msg) {
    if (body) {
      body.innerHTML =
        '<tr><td colspan="6" class="sales-empty sales-ov-loading">' +
        esc(msg) +
        "</td></tr>";
    }
    if (sub) sub.textContent = "نقاط البيع · " + msg;
    if (pill) pill.textContent = "…";
  }

  function fail(body, sub, pill, msg, elapsedMs) {
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
    var body = document.getElementById("sales-groups-body");
    var foot = document.getElementById("sales-groups-foot");
    var sub = document.getElementById("sales-groups-sub");
    var pill = document.getElementById("sales-groups-pill");
    var totInv = document.getElementById("sales-groups-tot-inv");
    var totQty = document.getElementById("sales-groups-tot-qty");
    var totSales = document.getElementById("sales-groups-tot-sales");
    var took = formatDuration(elapsedMs);

    if (!data || !data.ok || !data.groups) {
      fail(
        body,
        sub,
        pill,
        (data && data.error) || "تعذّر تحميل المجموعات",
        elapsedMs
      );
      return;
    }

    var rows = data.groups.rows || [];
    var totals = data.groups.totals || {};
    if (!rows.length) {
      if (body) {
        body.innerHTML =
          '<tr><td colspan="6" class="sales-empty">لا مبيعات مجموعات في الفترة.</td></tr>';
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
        (i + 1) +
        "</td>" +
        '<td title="' +
        esc(row.group_code) +
        '">' +
        esc(row.group_name) +
        "</td>" +
        '<td class="mono">' +
        esc(row.invoice_count_display) +
        "</td>" +
        '<td class="mono">' +
        esc(row.qty_display) +
        "</td>" +
        '<td class="mono sales-amt">' +
        moneyHtml(row.sales_total_display) +
        "</td>" +
        '<td class="mono">' +
        esc(row.share_display) +
        "</td>" +
        "</tr>";
    });
    if (body) body.innerHTML = html;
    if (totInv) totInv.textContent = totals.invoice_count_display || "0";
    if (totQty) totQty.textContent = totals.qty_display || "0";
    if (totSales) totSales.innerHTML = moneyHtml(totals.sales_total_display || "0.00");
    if (foot) foot.hidden = false;
    if (pill) pill.textContent = String(rows.length);
    if (sub) {
      sub.textContent =
        "نقاط البيع · " +
        (totals.group_count_display || rows.length) +
        " مجموعة · إجمالي " +
        (totals.sales_total_display || "0.00") +
        " · خلال " +
        took;
    }
  }

  function init() {
    var url = readUrl();
    if (!url) return;

    var body = document.getElementById("sales-groups-body");
    var sub = document.getElementById("sales-groups-sub");
    var pill = document.getElementById("sales-groups-pill");
    var started = Date.now();
    var tick = setInterval(function () {
      var elapsed = Date.now() - started;
      setLoading(
        body,
        sub,
        pill,
        "جاري تحميل مبيعات المجموعات… " + formatDuration(elapsed)
      );
    }, 1000);

    fetch(url, {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
      cache: "no-store",
    })
      .then(function (r) {
        var ct = (r.headers.get("content-type") || "").toLowerCase();
        if (!r.ok) {
          throw new Error("HTTP " + r.status);
        }
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
          body,
          sub,
          pill,
          (err && err.message) || "تعذّر تحميل المجموعات",
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
