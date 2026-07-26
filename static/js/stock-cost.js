(function () {
  "use strict";

  var form = document.getElementById("stock-cost-form");
  if (!form) return;

  var overlay = document.getElementById("stock-cost-progress");
  var fill = document.getElementById("stock-cost-progress-fill");
  var label = document.getElementById("stock-cost-progress-label");
  var statusEl = document.getElementById("stock-cost-progress-status");
  var titleEl = document.getElementById("stock-cost-progress-title");
  var closeBtn = document.getElementById("stock-cost-progress-close");
  var viewBtn = document.getElementById("stock-cost-btn");
  var refreshBtn = document.getElementById("stock-cost-refresh-btn");
  var actionInput = document.getElementById("stock-cost-action");
  var results = document.getElementById("stock-cost-results");
  var timer = null;
  var value = 0;
  var currentAction = "view";

  function setButtonsDisabled(disabled) {
    if (viewBtn) viewBtn.disabled = disabled;
    if (refreshBtn) refreshBtn.disabled = disabled;
  }

  function setProgress(n) {
    value = Math.max(1, Math.min(99, Math.round(n)));
    if (fill) fill.style.width = value + "%";
    if (label) label.textContent = value + "%";
  }

  function showOverlay(isRefresh) {
    if (!overlay) return;
    overlay.hidden = false;
    overlay.classList.remove("is-done", "is-error");
    if (closeBtn) closeBtn.hidden = true;
    if (titleEl) {
      titleEl.textContent = isRefresh ? "تحديث من النظام" : "عرض من التخزين";
    }
    if (statusEl) {
      var groupSelect = form.querySelector("#g_code");
      var groupLabel = groupSelect && groupSelect.value ? " للمجموعة المحددة" : "";
      statusEl.textContent = isRefresh
        ? "جاري جلب التكلفة من النظام" + groupLabel + "… قد يستغرق دقائق عند كل المجموعات."
        : "جاري تجميع الإجماليات من التخزين المحلي…";
    }
    setProgress(isRefresh ? 8 : 35);
  }

  function stopTimer() {
    if (timer) {
      window.clearInterval(timer);
      timer = null;
    }
  }

  function startTimer(isRefresh) {
    stopTimer();
    value = isRefresh ? 8 : 35;
    timer = window.setInterval(function () {
      if (value < 92) setProgress(value + Math.max(0.4, (92 - value) * (isRefresh ? 0.025 : 0.12)));
    }, isRefresh ? 400 : 120);
  }

  function escapeHtml(text) {
    return String(text == null ? "" : text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function renderReport(report) {
    if (!results || !report) return;
    var rowsHtml = (report.rows || [])
      .map(function (row) {
        return (
          "<tr>" +
          '<td class="mono">' + escapeHtml(row.g_code) + "</td>" +
          "<td>" + escapeHtml(row.g_name || "—") + "</td>" +
          '<td class="mono">' + escapeHtml(row.item_count) + "</td>" +
          '<td class="mono">' + escapeHtml(row.items_valued) + "</td>" +
          '<td class="mono">' + escapeHtml(row.total_qty_display) + "</td>" +
          '<td class="mono stock-cost-cell">' + escapeHtml(row.total_cost_display) + "</td>" +
          "</tr>"
        );
      })
      .join("");

    if (!rowsHtml) {
      rowsHtml = '<tr><td colspan="6">لا توجد مجموعات لعرضها.</td></tr>';
    }

    var titleExtra = report.g_code
      ? " | مجموعة " + escapeHtml(report.g_code) + (report.g_name ? " — " + escapeHtml(report.g_name) : "")
      : " | كل المجموعات";

    var sourceLabel = report.source === "cache" ? "من التخزين" : "بعد التحديث";
    var cacheLabel = report.cache_updated_display
      ? " | لقطة " + escapeHtml(report.cache_updated_display)
      : "";

    results.innerHTML =
      '<div class="panel panel-blue stock-cost-report">' +
      '<div class="panel-head stock-cost-report-head">' +
      '<span class="panel-ico" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M12 1v22M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/></svg></span>' +
      "<div><h3>إجمالي التكلفة — مخزن " + escapeHtml(report.warehouse) + titleExtra + "</h3>" +
      '<p class="panel-sub">' +
      escapeHtml(report.item_total) + " صنف | قُيّم " + escapeHtml(report.items_valued) +
      (report.errors ? " | تعذّر " + escapeHtml(report.errors) : "") +
      " | " + sourceLabel +
      " | خلال " + escapeHtml(report.elapsed_sec) + " ث" +
      cacheLabel +
      "</p></div>" +
      '<div class="stock-cost-grand"><span class="stock-cost-grand-label">الإجمالي</span>' +
      '<strong class="stock-cost-grand-value mono">' + escapeHtml(report.grand_total_display) + "</strong></div>" +
      "</div>" +
      '<div class="table-wrap"><table class="data-table stock-cost-table"><thead><tr>' +
      "<th>رقم المجموعة</th><th>اسم المجموعة</th><th>عدد الأصناف</th><th>أصناف قُيّمت</th><th>إجمالي الكمية</th><th>إجمالي التكلفة</th>" +
      "</tr></thead><tbody>" + rowsHtml + "</tbody><tfoot><tr>" +
      '<th colspan="5">الإجمالي الكلي</th><th class="mono">' +
      escapeHtml(report.grand_total_display) +
      "</th></tr></tfoot></table></div></div>";
  }

  function runAction(action) {
    currentAction = action === "refresh" ? "refresh" : "view";
    if (actionInput) actionInput.value = currentAction;
    setButtonsDisabled(true);
    showOverlay(currentAction === "refresh");
    startTimer(currentAction === "refresh");

    var body = new FormData(form);
    body.set("action", currentAction);

    fetch(form.action, {
      method: "POST",
      body: body,
      headers: {
        "X-Requested-With": "XMLHttpRequest",
        Accept: "application/json",
      },
      credentials: "same-origin",
    })
      .then(function (res) {
        return res.text().then(function (text) {
          var data = null;
          try {
            data = JSON.parse(text);
          } catch (e) {
            throw new Error(
              res.status === 403
                ? "تم رفض الطلب. أعد تسجيل الدخول ثم حاول مجدداً."
                : "استجابة غير متوقعة من الخادم."
            );
          }
          if (!res.ok || !data || !data.ok) {
            throw new Error((data && data.error) || "فشل حساب التكلفة.");
          }
          return data;
        });
      })
      .then(function (data) {
        stopTimer();
        setProgress(100);
        if (overlay) overlay.classList.add("is-done");
        if (statusEl) {
          statusEl.textContent =
            currentAction === "refresh"
              ? "اكتمل التحديث وحُفظت اللقطة محلياً."
              : "اكتمل العرض من التخزين.";
        }
        renderReport(data.report);
        window.setTimeout(function () {
          if (overlay) overlay.hidden = true;
          setButtonsDisabled(false);
        }, 450);
      })
      .catch(function (err) {
        stopTimer();
        if (overlay) overlay.classList.add("is-error");
        if (statusEl) statusEl.textContent = err.message || "حدث خطأ أثناء الحساب.";
        if (label) label.textContent = "!";
        if (closeBtn) closeBtn.hidden = false;
        setButtonsDisabled(false);
      });
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    var submitter = event.submitter;
    var action =
      (submitter && submitter.getAttribute("data-action")) ||
      (actionInput && actionInput.value) ||
      "view";
    runAction(action);
  });

  if (closeBtn) {
    closeBtn.addEventListener("click", function () {
      if (overlay) overlay.hidden = true;
      setButtonsDisabled(false);
    });
  }
})();
