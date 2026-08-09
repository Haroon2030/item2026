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
    return "";
  }

  function readSeed() {
    var el = document.getElementById("sales-groups-seed");
    if (!el) return null;
    try {
      var data = JSON.parse(el.textContent || "null");
      if (data && data.ok && data.groups) return data;
    } catch (e) {
      /* ignore */
    }
    return null;
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
        '<tr><td colspan="6" class="sales-empty">' +
        esc(msg) +
        ' <button type="button" class="btn-primary sales-groups-retry" style="margin-inline-start:0.5rem">إعادة المحاولة</button>' +
        "</td></tr>";
      var btn = body.querySelector(".sales-groups-retry");
      if (btn) {
        btn.addEventListener("click", function () {
          loadSingle(readUrl(), 1);
        });
      }
    }
    if (pill) pill.textContent = "!";
    if (sub) {
      sub.textContent =
        "نقاط البيع · فشل التحميل · بعد " + formatDuration(elapsedMs);
    }
  }

  function render(data, elapsedMs, progressNote) {
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
    var warn = (data.groups && data.groups.warning) || data.warning || "";
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
        took +
        (progressNote ? " · " + progressNote : "") +
        (warn ? " · " + warn : "");
    }
  }

  function friendlyFetchError(err) {
    if (!err) return "تعذّر تحميل المجموعات";
    if (err.name === "AbortError") {
      return "انتهت مهلة التحميل — أوراكل بطيء أو غير مستجيب";
    }
    var raw = String(err.message || "");
    var low = raw.toLowerCase();
    if (/مهلة|timeout|timed out/i.test(raw)) {
      return "انتهت مهلة جلب مبيعات المجموعات من أوراكل. أعد المحاولة بعد لحظات.";
    }
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

  function fetchJson(url, timeoutMs) {
    var ctrl = typeof AbortController !== "undefined" ? new AbortController() : null;
    var abortTimer = null;
    if (ctrl) {
      abortTimer = setTimeout(function () {
        try {
          ctrl.abort();
        } catch (e) {
          /* ignore */
        }
      }, timeoutMs || 4 * 60 * 1000);
    }
    return fetch(url, {
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
          if (r.redirected && /\/login\/?/i.test(r.url || "")) {
            throw new Error("انتهت الجلسة — سجّل الدخول ثم أعد فتح الصفحة");
          }
          if (!r.ok || (data && data.ok === false)) {
            throw new Error(
              (data && data.error) ||
                (r.ok ? "تعذّر تحميل المجموعات" : "HTTP " + r.status)
            );
          }
          if (!data) throw new Error("استجابة غير JSON");
          return data;
        });
      })
      .finally(function () {
        if (abortTimer) clearTimeout(abortTimer);
      });
  }

  function finishSignals() {
    try {
      window.dispatchEvent(new Event("sales-groups-first"));
      window.dispatchEvent(new Event("sales-groups-done"));
    } catch (e) {
      /* ignore */
    }
  }

  function loadSingle(url, attempt) {
    if (!url) return;
    var body = document.getElementById("sales-groups-body");
    var sub = document.getElementById("sales-groups-sub");
    var pill = document.getElementById("sales-groups-pill");
    var started = Date.now();
    var tryNo = attempt || 1;
    var maxTries = 2;

    var tick = setInterval(function () {
      setLoading(
        body,
        sub,
        pill,
        "جاري تحميل مبيعات المجموعات… " + formatDuration(Date.now() - started)
      );
    }, 1000);

    fetchJson(url, 180 * 1000)
      .then(function (data) {
        clearInterval(tick);
        finishSignals();
        render(data, Date.now() - started);
      })
      .catch(function (err) {
        clearInterval(tick);
        var msg = friendlyFetchError(err);
        var isRetryable =
          /انقطع|failed to fetch|network|مهلة|timeout|abort/i.test(
            String((err && err.message) || "") + " " + msg
          );
        if (isRetryable && tryNo < maxTries) {
          setLoading(
            body,
            sub,
            pill,
            "تعثّر التحميل — إعادة المحاولة " + (tryNo + 1) + "/" + maxTries + "…"
          );
          setTimeout(function () {
            loadSingle(url, tryNo + 1);
          }, 1500);
          return;
        }
        finishSignals();
        fail(body, sub, pill, msg, Date.now() - started);
      });
  }

  function init() {
    var url = readUrl();
    var seeded = readSeed();
    if (seeded) {
      render(seeded, 0, "من الكاش");
      finishSignals();
      // تحديث صامت مرة واحدة — بلا حلقة شهور
      if (url) {
        fetchJson(url, 180 * 1000)
          .then(function (data) {
            render(data, 0, "محدّث");
          })
          .catch(function () {
            /* أبقِ الكاش المعروض */
          });
      }
      return;
    }
    if (!url) return;
    loadSingle(url, 1);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
