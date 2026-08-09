(function () {
  "use strict";

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
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
    var seed = document.getElementById("sales-activity-url");
    if (!seed) return "";
    try {
      return String(JSON.parse(seed.textContent || '""') || "");
    } catch (e) {
      return "";
    }
  }

  function continuityClass(pct) {
    var n = Number(pct) || 0;
    if (n >= 70) return "sales-cont-high";
    if (n >= 50) return "sales-cont-mid";
    if (n >= 30) return "sales-cont-ok";
    return "sales-cont-low";
  }

  function setLoading(msg) {
    var body = document.getElementById("sales-activity-body");
    var sub = document.getElementById("sales-activity-sub");
    var pill = document.getElementById("sales-activity-pill");
    if (body) {
      body.innerHTML =
        '<tr><td colspan="7" class="sales-empty sales-ov-loading">' +
        esc(msg) +
        "</td></tr>";
    }
    if (sub) sub.textContent = "ساعات البيع · " + msg;
    if (pill) pill.textContent = "…";
  }

  function fail(msg, elapsedMs) {
    var body = document.getElementById("sales-activity-body");
    var sub = document.getElementById("sales-activity-sub");
    var pill = document.getElementById("sales-activity-pill");
    if (body) {
      body.innerHTML =
        '<tr><td colspan="7" class="sales-empty">' + esc(msg) + "</td></tr>";
    }
    if (pill) pill.textContent = "!";
    if (sub) {
      sub.textContent =
        "ساعات البيع · فشل التحميل · بعد " + formatDuration(elapsedMs);
    }
  }

  function render(data, elapsedMs) {
    var body = document.getElementById("sales-activity-body");
    var foot = document.getElementById("sales-activity-foot");
    var sub = document.getElementById("sales-activity-sub");
    var pill = document.getElementById("sales-activity-pill");
    var totBr = document.getElementById("sales-activity-tot-branches");
    var totInv = document.getElementById("sales-activity-tot-inv");
    var took = formatDuration(elapsedMs);

    if (!data || !data.ok || !data.activity) {
      fail((data && data.error) || "تعذّر تحميل نشاط الفروع", elapsedMs);
      return;
    }

    var rows = data.activity.rows || [];
    var totals = data.activity.totals || {};

    if (!rows.length) {
      if (body) {
        body.innerHTML =
          '<tr><td colspan="7" class="sales-empty">لا مبيعات فروع في الفترة.</td></tr>';
      }
      if (foot) foot.hidden = true;
      if (pill) pill.textContent = "0";
      if (sub) sub.textContent = "ساعات البيع · لا بيانات · خلال " + took;
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
        esc(row.branch_code) +
        '">' +
        esc(row.branch_name) +
        "</td>" +
        '<td class="mono">' +
        esc(row.avg_hours_display) +
        "</td>" +
        '<td class="mono">' +
        esc(row.hours_span_display) +
        "</td>" +
        '<td class="mono">' +
        esc(row.active_days_display) +
        "</td>" +
        '<td class="mono">' +
        esc(row.invoice_count_display) +
        "</td>" +
        '<td class="mono ' +
        continuityClass(row.continuity_pct) +
        '" title="' +
        esc(row.continuity_display) +
        '">' +
        esc(row.continuity_label) +
        "</td>" +
        "</tr>";
    });
    if (body) body.innerHTML = html;
    if (totBr) {
      totBr.textContent = (totals.branch_count_display || rows.length) + " فرع";
    }
    if (totInv) totInv.textContent = totals.invoice_count_display || "0";
    if (foot) foot.hidden = false;
    if (pill) pill.textContent = String(rows.length);
    if (sub) {
      sub.textContent =
        "ساعات البيع · مرتّب حسب الاستمرارية · " +
        rows.length +
        " فرع · خلال " +
        took;
    }
  }

  function loadActivity(url, attempt) {
    var started = Date.now();
    var tryNo = attempt || 1;
    var maxTries = 3;

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
      }, 3 * 60 * 1000);
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
                (r.ok ? "تعذّر تحميل نشاط الفروع" : "HTTP " + r.status)
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
            loadActivity(url, tryNo + 1);
          }, 1500 * tryNo);
          return;
        }
        var msg = raw || "تعذّر تحميل نشاط الفروع";
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

    // طلب واحد فوري — بلا حلقات انتظار
    loadActivity(url, 1);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
