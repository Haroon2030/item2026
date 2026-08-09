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
    var seed = document.getElementById("sales-users-url");
    if (!seed) return "";
    try {
      return String(JSON.parse(seed.textContent || '""') || "");
    } catch (e) {
      return "";
    }
  }

  function setLoading(msg) {
    var body = document.getElementById("sales-users-body");
    var sub = document.getElementById("sales-users-sub");
    var pill = document.getElementById("sales-users-pill");
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
    var body = document.getElementById("sales-users-body");
    var sub = document.getElementById("sales-users-sub");
    var pill = document.getElementById("sales-users-pill");
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
    var body = document.getElementById("sales-users-body");
    var foot = document.getElementById("sales-users-foot");
    var sub = document.getElementById("sales-users-sub");
    var pill = document.getElementById("sales-users-pill");
    var totInv = document.getElementById("sales-users-tot-inv");
    var totSales = document.getElementById("sales-users-tot-sales");
    var took = formatDuration(elapsedMs);

    if (!data || !data.ok || !data.users) {
      fail((data && data.error) || "تعذّر تحميل أكثر المستخدمين بيعاً", elapsedMs);
      return;
    }

    var rows = data.users.rows || [];
    var totals = data.users.totals || {};

    if (!rows.length) {
      if (body) {
        body.innerHTML =
          '<tr><td colspan="6" class="sales-empty">لا مبيعات مستخدمين في الفترة.</td></tr>';
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
        esc(row.user_code) +
        '">' +
        esc(row.user_name) +
        ' <small class="mono">#' +
        esc(row.user_code) +
        "</small></td>" +
        '<td class="mono">' +
        esc(row.invoice_count_display) +
        "</td>" +
        '<td class="mono sales-col-cost">' +
        esc(row.avg_basket_display) +
        "</td>" +
        '<td class="mono sales-amt sales-col-amt">' +
        moneyHtml(row.sales_total_display) +
        "</td>" +
        '<td class="mono sales-col-share">' +
        esc(row.share_display) +
        "</td>" +
        "</tr>";
    });
    if (body) body.innerHTML = html;
    if (totInv) totInv.textContent = totals.invoice_count_display || "0";
    if (totSales) totSales.innerHTML = moneyHtml(totals.sales_total_display || "0.00");
    if (foot) foot.hidden = false;
    if (pill) pill.textContent = String(rows.length);
    if (sub) {
      sub.textContent =
        "نقاط البيع · أعلى " +
        rows.length +
        " مستخدم · إجمالي " +
        (totals.sales_total_display || "0.00") +
        " · خلال " +
        took;
    }
  }

  function loadUsers(url, attempt) {
    var started = Date.now();
    var tryNo = attempt || 1;
    var maxTries = 2;

    setLoading("جاري التحميل… 0ث");
    var tick = setInterval(function () {
      setLoading("جاري التحميل… " + formatDuration(Date.now() - started));
    }, 1000);

    var ctrl = typeof AbortController !== "undefined" ? new AbortController() : null;
    var abortTimer = null;
    if (ctrl) {
      abortTimer = setTimeout(function () {
        try {
          ctrl.abort();
        } catch (e) {
          /* ignore */
        }
      }, 90 * 1000);
    }

    fetch(url, {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
      cache: "no-store",
      signal: ctrl ? ctrl.signal : undefined,
    })
      .then(function (r) {
        return r.text().then(function (text) {
          var data = null;
          try {
            data = text ? JSON.parse(text) : null;
          } catch (e) {
            data = null;
          }
          if (!r.ok || (data && data.ok === false)) {
            throw new Error(
              (data && data.error) ||
                (r.ok ? "تعذّر تحميل أكثر المستخدمين بيعاً" : "HTTP " + r.status)
            );
          }
          if (!data) throw new Error("استجابة غير JSON");
          return data;
        });
      })
      .then(function (data) {
        clearInterval(tick);
        if (abortTimer) clearTimeout(abortTimer);
        render(data, Date.now() - started);
      })
      .catch(function (err) {
        clearInterval(tick);
        if (abortTimer) clearTimeout(abortTimer);
        var raw = String((err && err.message) || "");
        var isNet = /failed to fetch|network|انقطع/i.test(raw);
        if (isNet && tryNo < maxTries) {
          setLoading(
            "انقطع الاتصال — إعادة المحاولة " + (tryNo + 1) + "/" + maxTries + "…"
          );
          setTimeout(function () {
            loadUsers(url, tryNo + 1);
          }, 1500 * tryNo);
          return;
        }
        var msg = raw || "تعذّر تحميل أكثر المستخدمين بيعاً";
        if (err && err.name === "AbortError") {
          msg = "انتهت مهلة التحميل — أوراكل بطيء أو غير مستجيب";
        } else if (isNet) {
          msg = "انقطع الاتصال بالسيرفر. حدّث الصفحة (Ctrl+F5)";
        }
        fail(msg, Date.now() - started);
      });
  }

  function init() {
    var url = readUrl();
    if (!url) return;
    loadUsers(url, 1);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
