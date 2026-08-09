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

  function friendlyFetchError(err) {
    if (!err) return "تعذّر تحميل المجموعات";
    if (err.name === "AbortError") {
      return "انتهت مهلة التحميل — أوراكل بطيء أو غير مستجيب";
    }
    var raw = String(err.message || "");
    var low = raw.toLowerCase();
    if (
      low === "failed to fetch" ||
      low.indexOf("networkerror") !== -1 ||
      low.indexOf("network request failed") !== -1 ||
      low.indexOf("load failed") !== -1
    ) {
      return "انقطع الاتصال بالسيرفر — جاري إعادة المحاولة…";
    }
    return raw || "تعذّر تحميل المجموعات";
  }

  function loadGroups(url, attempt) {
    var body = document.getElementById("sales-groups-body");
    var sub = document.getElementById("sales-groups-sub");
    var pill = document.getElementById("sales-groups-pill");
    var started = Date.now();
    var tryNo = attempt || 1;
    var maxTries = 3;

    var tick = setInterval(function () {
      var elapsed = Date.now() - started;
      setLoading(
        body,
        sub,
        pill,
        "جاري تحميل مبيعات المجموعات… " + formatDuration(elapsed)
      );
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
      }, 6 * 60 * 1000);
    }

    function done() {
      clearInterval(tick);
      if (abortTimer) clearTimeout(abortTimer);
    }

    function finishAll() {
      done();
      try {
        window.dispatchEvent(new Event("sales-groups-done"));
      } catch (e) {
        /* ignore */
      }
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
          // جلسة منتهية → إعادة توجيه لتسجيل الدخول
          if (r.redirected && /\/login\/?/i.test(r.url || "")) {
            throw new Error("انتهت الجلسة — سجّل الدخول ثم أعد فتح الصفحة");
          }
          if (!r.ok || (data && data.ok === false)) {
            throw new Error(
              (data && data.error) ||
                (r.ok ? "تعذّر تحميل المجموعات" : "HTTP " + r.status)
            );
          }
          if (!data) {
            throw new Error("استجابة غير JSON");
          }
          return data;
        });
      })
      .then(function (data) {
        finishAll();
        render(data, Date.now() - started);
      })
      .catch(function (err) {
        done();
        var isAbort = err && err.name === "AbortError";
        var msg = friendlyFetchError(err);
        var isNet =
          !isAbort &&
          /انقطع الاتصال|failed to fetch|network/i.test(
            String((err && err.message) || "") + " " + msg
          );

        if (isNet && tryNo < maxTries) {
          setLoading(
            body,
            sub,
            pill,
            "انقطع الاتصال — إعادة المحاولة " + (tryNo + 1) + "/" + maxTries + "…"
          );
          setTimeout(function () {
            loadGroups(url, tryNo + 1);
          }, 1500 * tryNo);
          return;
        }

        if (isNet) {
          msg =
            "انقطع الاتصال بالسيرفر. تأكد أن السيرفر يعمل ثم حدّث الصفحة (Ctrl+F5)";
        }
        finishAll();
        fail(body, sub, pill, msg, Date.now() - started);
      });
  }

  function init() {
    var url = readUrl();
    if (!url) return;
    loadGroups(url, 1);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
