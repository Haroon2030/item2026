(function () {
  "use strict";

  var refreshBtn = document.getElementById("btn-refresh");
  if (refreshBtn) {
    refreshBtn.addEventListener("click", function () {
      refreshBtn.classList.add("is-spinning");
      refreshBtn.disabled = true;
      window.location.reload();
    });
  }
})();

(function () {
  "use strict";

  var form = document.querySelector(".sync-form");
  if (!form) return;

  var overlay = document.getElementById("sync-progress");
  var fill = document.getElementById("sync-progress-fill");
  var label = document.getElementById("sync-progress-label");
  var statusEl = document.getElementById("sync-progress-status");
  var closeBtn = document.getElementById("sync-progress-close");
  var button = form.querySelector('button[type="submit"]');

  if (!overlay || !fill || !label || !statusEl) return;

  var timer = null;
  var value = 0;

  function setProgress(n) {
    value = Math.max(1, Math.min(100, Math.round(n)));
    fill.style.width = value + "%";
    label.textContent = value + "%";
    overlay.setAttribute("aria-valuenow", String(value));
  }

  function showOverlay() {
    overlay.hidden = false;
    overlay.classList.remove("is-done", "is-error");
    if (closeBtn) closeBtn.hidden = true;
    statusEl.textContent = "جاري مزامنة الفهرس من النظام…";
    setProgress(1);
  }

  function stopTimer() {
    if (timer) {
      window.clearInterval(timer);
      timer = null;
    }
  }

  function startFakeProgress() {
    stopTimer();
    value = 1;
    setProgress(1);
    timer = window.setInterval(function () {
      if (value >= 90) return;
      var step = value < 40 ? 2.2 : value < 70 ? 1.2 : 0.45;
      setProgress(value + step);
    }, 350);
  }

  function parseResponse(res) {
    return res.text().then(function (text) {
      var data = null;
      var trimmed = (text || "").trim();
      if (trimmed) {
        try {
          data = JSON.parse(trimmed);
        } catch (e) {
          if (res.status === 403 || /login|تسجيل الدخول/i.test(trimmed)) {
            throw new Error("انتهت الجلسة. أعد تسجيل الدخول ثم حاول المزامنة.");
          }
          if (res.status === 429) {
            throw new Error("تم تجاوز حد المزامنة. حاول لاحقاً.");
          }
          throw new Error(
            "فشلت المزامنة (استجابة غير متوقعة من الخادم، رمز " +
              res.status +
              ")."
          );
        }
      }
      return { okHttp: res.ok, status: res.status, data: data };
    });
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();

    if (button) button.disabled = true;
    showOverlay();
    startFakeProgress();

    var body = new FormData(form);

    fetch(form.action, {
      method: "POST",
      body: body,
      headers: {
        "X-Requested-With": "XMLHttpRequest",
        Accept: "application/json",
      },
      credentials: "same-origin",
      redirect: "manual",
    })
      .then(function (res) {
        // إعادة توجيه للدخول = الجلسة انتهت
        if (res.type === "opaqueredirect" || res.status === 0 || (res.status >= 300 && res.status < 400)) {
          throw new Error("انتهت الجلسة. أعد تسجيل الدخول ثم حاول المزامنة.");
        }
        return parseResponse(res);
      })
      .then(function (result) {
        stopTimer();
        if (!result.data || result.data.ok !== true) {
          throw new Error(
            (result.data && result.data.error) || "فشلت المزامنة."
          );
        }
        setProgress(100);
        overlay.classList.add("is-done");
        statusEl.textContent = result.data.message || "اكتملت المزامنة بنجاح.";
        window.setTimeout(function () {
          window.location.reload();
        }, 700);
      })
      .catch(function (err) {
        stopTimer();
        setProgress(Math.max(value, 1));
        overlay.classList.add("is-error");
        statusEl.textContent = err.message || "حدث خطأ أثناء المزامنة.";
        if (closeBtn) closeBtn.hidden = false;
        if (button) button.disabled = false;
      });
  });

  if (closeBtn) {
    closeBtn.addEventListener("click", function () {
      overlay.hidden = true;
      if (button) button.disabled = false;
    });
  }
})();
