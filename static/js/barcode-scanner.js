(function () {
  "use strict";

  var btn = document.getElementById("btn-scan");
  var overlay = document.getElementById("barcode-scanner");
  var readerEl = document.getElementById("barcode-reader");
  var statusEl = document.getElementById("barcode-scanner-status");
  var closeBtn = document.getElementById("barcode-scanner-close");
  var input = document.getElementById("q");
  var form = document.querySelector(".search-form");

  if (!btn || !overlay || !readerEl || !statusEl || !input || !form) return;

  var scanner = null;
  var handled = false;

  function setStatus(text, kind) {
    statusEl.textContent = text;
    statusEl.classList.remove("is-error", "is-ok");
    if (kind === "error") statusEl.classList.add("is-error");
    if (kind === "ok") statusEl.classList.add("is-ok");
  }

  function stopScanner() {
    handled = false;
    if (!scanner) return Promise.resolve();
    var current = scanner;
    scanner = null;
    return current
      .stop()
      .then(function () {
        return current.clear();
      })
      .catch(function () {
        /* ignore stop errors */
      });
  }

  function closeScanner() {
    stopScanner().finally(function () {
      overlay.hidden = true;
      readerEl.innerHTML = "";
    });
  }

  function onDetected(code) {
    if (handled || !code) return;
    handled = true;
    var value = String(code).trim();
    setStatus("تم التقاط: " + value, "ok");
    input.value = value;
    stopScanner().finally(function () {
      overlay.hidden = true;
      readerEl.innerHTML = "";
      form.submit();
    });
  }

  function openScanner() {
    if (!window.isSecureContext) {
      overlay.hidden = false;
      setStatus("الكاميرا تحتاج HTTPS. افتح: https://item.alrsheed.net", "error");
      return;
    }

    if (typeof Html5Qrcode === "undefined") {
      overlay.hidden = false;
      setStatus("مكتبة المسح غير محمّلة. حدّث الصفحة ثم أعد المحاولة.", "error");
      return;
    }

    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      overlay.hidden = false;
      setStatus("الكاميرا غير متاحة في هذا المتصفح.", "error");
      return;
    }

    handled = false;
    overlay.hidden = false;
    readerEl.innerHTML = "";
    setStatus("جاري فتح الكاميرا… اسمح بالإذن إن طُلب منك.");

    var formats = undefined;
    try {
      if (typeof Html5QrcodeSupportedFormats !== "undefined") {
        formats = [
          Html5QrcodeSupportedFormats.EAN_13,
          Html5QrcodeSupportedFormats.EAN_8,
          Html5QrcodeSupportedFormats.CODE_128,
          Html5QrcodeSupportedFormats.CODE_39,
          Html5QrcodeSupportedFormats.UPC_A,
          Html5QrcodeSupportedFormats.UPC_E,
          Html5QrcodeSupportedFormats.QR_CODE,
          Html5QrcodeSupportedFormats.ITF,
        ];
      }
    } catch (e) {
      formats = undefined;
    }

    try {
      scanner = formats
        ? new Html5Qrcode("barcode-reader", { formatsToSupport: formats })
        : new Html5Qrcode("barcode-reader");
    } catch (err) {
      setStatus("تعذر تهيئة قارئ الباركود.", "error");
      return;
    }

    var config = {
      fps: 10,
      qrbox: function (viewfinderWidth, viewfinderHeight) {
        var side = Math.floor(Math.min(viewfinderWidth, viewfinderHeight) * 0.72);
        return { width: side, height: Math.floor(side * 0.55) };
      },
      aspectRatio: 1.333,
      experimentalFeatures: {
        useBarCodeDetectorIfSupported: true,
      },
    };

    scanner
      .start(
        { facingMode: "environment" },
        config,
        function (decodedText) {
          onDetected(decodedText);
        },
        function () {
          /* ignore frame miss */
        }
      )
      .then(function () {
        setStatus("وجّه الكاميرا نحو الباركود…");
      })
      .catch(function (err) {
        var msg = "تعذر فتح الكاميرا.";
        var name = err && (err.name || err);
        var text = (err && err.message) || String(err || "");
        if (name === "NotAllowedError" || /Permission|NotAllowed/i.test(text)) {
          msg = "تم رفض إذن الكاميرا. من إعدادات المتصفح اسمح بالكاميرا لهذا الموقع.";
        } else if (name === "NotFoundError" || /Requested device not found/i.test(text)) {
          msg = "لم يتم العثور على كاميرا.";
        } else if (name === "NotReadableError" || /Could not start video/i.test(text)) {
          msg = "الكاميرا مستخدمة من تطبيق آخر. أغلقه ثم أعد المحاولة.";
        } else if (text) {
          msg = "تعذر فتح الكاميرا: " + text;
        }
        setStatus(msg, "error");
        stopScanner();
      });
  }

  btn.addEventListener("click", function () {
    openScanner();
  });

  if (closeBtn) {
    closeBtn.addEventListener("click", closeScanner);
  }

  overlay.addEventListener("click", function (event) {
    if (event.target === overlay) closeScanner();
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && !overlay.hidden) closeScanner();
  });
})();
