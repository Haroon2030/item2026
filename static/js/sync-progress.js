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
      // يتقدم بسرعة في البداية ثم يبطئ قبل 90%
      var step = value < 40 ? 2.2 : value < 70 ? 1.2 : 0.45;
      setProgress(value + step);
    }, 350);
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
    })
      .then(function (res) {
        return res.json().then(function (data) {
          return { okHttp: res.ok, status: res.status, data: data };
        });
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
