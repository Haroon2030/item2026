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

  function readMonthApiBase() {
    var seed = document.getElementById("sales-groups-month-url");
    if (!seed) return "";
    try {
      return String(JSON.parse(seed.textContent || '""') || "");
    } catch (e) {
      return "";
    }
  }

  function monthApiUrl(base, dateFrom, dateTo) {
    if (!base) return "";
    var join = base.indexOf("?") >= 0 ? "&" : "?";
    return (
      base +
      join +
      "date_from=" +
      encodeURIComponent(dateFrom) +
      "&date_to=" +
      encodeURIComponent(dateTo)
    );
  }

  function runPool(jobs, concurrency, worker) {
    var i = 0;
    var running = 0;
    var done = 0;
    return new Promise(function (resolve) {
      function pump() {
        while (running < concurrency && i < jobs.length) {
          (function (job, idx) {
            running += 1;
            Promise.resolve()
              .then(function () {
                return worker(job, idx);
              })
              .catch(function () {
                /* ignore one month fail */
              })
              .then(function () {
                running -= 1;
                done += 1;
                if (done >= jobs.length) resolve();
                else pump();
              });
          })(jobs[i], i);
          i += 1;
        }
        if (jobs.length === 0) resolve();
      }
      pump();
    });
  }

  function fetchSqlMonthsThenReload(mainUrl, data, started) {
    var months = (data && data.groups && data.groups.sql_months) || [];
    var monthBase = readMonthApiBase();
    if (!months.length || !monthBase) {
      maybePollRefresh(mainUrl, data);
      return;
    }
    var body = document.getElementById("sales-groups-body");
    var sub = document.getElementById("sales-groups-sub");
    var pill = document.getElementById("sales-groups-pill");
    var total = months.length;
    var finished = 0;
    setStatus("warming", "SQL " + finished + "/" + total);
    if (sub) {
      sub.textContent =
        "نقاط البيع · جلب SQL متوازٍ للشهور… 0/" + total;
    }
    runPool(months, 3, function (m) {
      var u = monthApiUrl(monthBase, m.date_from, m.date_to);
      return fetchJson(u, 150 * 1000).then(function () {
        finished += 1;
        setStatus("warming", "SQL " + finished + "/" + total);
        if (sub) {
          sub.textContent =
            "نقاط البيع · جلب SQL متوازٍ… " +
            finished +
            "/" +
            total +
            " · " +
            formatDuration(Date.now() - started);
        }
      });
    }).then(function () {
      return fetchJson(mainUrl, 120 * 1000).then(function (fresh) {
        render(fresh, Date.now() - started);
        var still =
          !!(fresh && fresh.groups && fresh.groups.incomplete) ||
          ((fresh.groups && fresh.groups.sql_months) || []).length > 0;
        if (still) {
          maybePollRefresh(mainUrl, fresh);
        }
      });
    }).catch(function (err) {
      fail(
        body,
        sub,
        pill,
        friendlyFetchError(err),
        Date.now() - started
      );
    });
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
    setStatus("loading", "جاري التحميل…");
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
    setStatus("error", "غير مكتمل");
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

  function setStatus(kind, label) {
    var el = document.getElementById("sales-groups-status");
    if (!el) return;
    el.className = "sales-groups-status is-" + (kind || "ready");
    el.textContent = label || "";
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
      setStatus("error", "غير مكتمل");
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
    var incomplete = !!(data.groups && data.groups.incomplete);
    var matched = data.groups && data.groups.matched === true;
    var stillWarming =
      incomplete ||
      (!matched &&
        /خلفية|شهر|جزئي|أقل من جدول|تجهيز|لا يطابق|بدون مطابقة/i.test(
          String(warn || "")
        ));

    if (!rows.length) {
      if (body) {
        if (warn) {
          body.innerHTML =
            '<tr><td colspan="6" class="sales-empty">' +
            esc(warn) +
            ' <button type="button" class="btn-primary sales-groups-retry" style="margin-inline-start:0.5rem">إعادة المحاولة</button>' +
            "</td></tr>";
          var retryBtn = body.querySelector(".sales-groups-retry");
          if (retryBtn) {
            retryBtn.addEventListener("click", function () {
              loadSingle(readUrl(), 1);
            });
          }
          setStatus("warming", "جارٍ الإكمال…");
        } else {
          body.innerHTML =
            '<tr><td colspan="6" class="sales-empty">لا مبيعات مجموعات في الفترة.</td></tr>';
          setStatus("ready", "مكتمل");
        }
      }
      if (foot) foot.hidden = true;
      if (pill) pill.textContent = warn ? "!" : "0";
      if (sub) {
        sub.textContent = warn
          ? "نقاط البيع · " + warn + " · خلال " + took
          : "نقاط البيع · لا بيانات · خلال " + took;
      }
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

    if (stillWarming || incomplete) {
      setStatus("warming", "جارٍ الإكمال…");
    } else if (matched) {
      setStatus("ready", "مكتمل ومطابق ✓");
    } else if (warn && /يطابق|مطابقة/i.test(String(warn))) {
      setStatus("warming", "غير مطابق للفروع");
    } else {
      setStatus("ready", "مكتمل");
    }

    if (sub) {
      var posNote = "";
      if (data.groups && data.groups.pos_total_display) {
        posNote = " · فروع " + data.groups.pos_total_display;
      }
      var cache = (data.groups && data.groups.cache) || {};
      var mReady = Number(cache.months_ready || 0);
      var mTotal = Number(cache.months_total || 0);
      var cacheNote =
        mTotal > 1 ? " · JSON " + mReady + "/" + mTotal + " شهر" : "";
      var stateNote = stillWarming || incomplete
        ? " · يُجلب SQL ويُجمَّع…"
        : matched
          ? " · مطابق لجدول الفروع"
          : warn && /يطابق|مطابقة/i.test(String(warn))
            ? " · غير مطابق للفروع"
            : "";
      sub.textContent =
        "نقاط البيع · " +
        (totals.group_count_display || rows.length) +
        " مجموعة · إجمالي " +
        (totals.sales_total_display || "0.00") +
        posNote +
        cacheNote +
        " · خلال " +
        took +
        stateNote +
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
    if (/\bHTTP\s*50[234]\b/i.test(raw) || /\b502\b|\b503\b|\b504\b/.test(raw)) {
      return "السيرفر لم يُكمِل الطلب (بوابة/مهلة). أعد المحاولة بعد لحظات.";
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

  function isRetryableError(err, msg) {
    var blob = String((err && err.message) || "") + " " + String(msg || "");
    return /انقطع|failed to fetch|network|مهلة|timeout|abort|502|503|504|بوابة|gateway/i.test(
      blob
    );
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
    var maxTries = 3;

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
        fetchSqlMonthsThenReload(url, data, started);
      })
      .catch(function (err) {
        clearInterval(tick);
        var msg = friendlyFetchError(err);
        if (isRetryableError(err, msg) && tryNo < maxTries) {
          setLoading(
            body,
            sub,
            pill,
            "تعثّر التحميل — إعادة المحاولة " + (tryNo + 1) + "/" + maxTries + "…"
          );
          setTimeout(function () {
            loadSingle(url, tryNo + 1);
          }, 1200 * tryNo);
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
      // تحديث صامت مرة واحدة — إن ناقص شهور يُجلب SQL متوازٍ
      if (url) {
        fetchJson(url, 180 * 1000)
          .then(function (data) {
            render(data, 0, "محدّث");
            fetchSqlMonthsThenReload(url, data, Date.now());
          })
          .catch(function () {
            /* أبقِ الكاش المعروض */
            fetchSqlMonthsThenReload(url, seeded, Date.now());
          });
      }
      return;
    }
    if (!url) return;
    loadSingle(url, 1);
  }

  function maybePollRefresh(url, data) {
    var incomplete = !!(data && data.groups && data.groups.incomplete);
    var matched = !!(data && data.groups && data.groups.matched);
    var longRange = !!(data && data.groups && data.groups.long_range);
    var warn =
      (data && data.groups && data.groups.warning) || (data && data.warning) || "";
    // استمر حتى المطابقة أو انتهاء التدفئة — لا تتوقف عند «مكتمل» وهمي
    var needPoll =
      incomplete ||
      (longRange && !matched) ||
      (!matched &&
        /خلفية|شهر|جزئي|أقل من جدول|تجهيز|يطابق|مطابقة/i.test(String(warn || "")));
    if (!needPoll) return;
    var tries = 0;
    var maxPolls = 24;
    function poll() {
      tries += 1;
      fetchJson(url, 150 * 1000)
        .then(function (fresh) {
          render(fresh, 0);
          var stillIncomplete = !!(fresh && fresh.groups && fresh.groups.incomplete);
          var nowMatched = !!(fresh && fresh.groups && fresh.groups.matched);
          if ((!nowMatched || stillIncomplete) && tries < maxPolls) {
            setTimeout(poll, 3500);
          }
        })
        .catch(function () {
          if (tries < maxPolls) setTimeout(poll, 6000);
        });
    }
    setTimeout(poll, 2500);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
