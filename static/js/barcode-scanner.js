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
  var nativeTimer = null;

  function setStatus(text, kind) {
    statusEl.textContent = text;
    statusEl.classList.remove("is-error", "is-ok");
    if (kind === "error") statusEl.classList.add("is-error");
    if (kind === "ok") statusEl.classList.add("is-ok");
  }

  function stopNativeLoop() {
    if (nativeTimer) {
      window.clearInterval(nativeTimer);
      nativeTimer = null;
    }
  }

  function stopScanner() {
    stopNativeLoop();
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
        /* ignore */
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
    if (!value) return;
    setStatus("تم التقاط: " + value, "ok");
    input.value = value;
    stopScanner().finally(function () {
      overlay.hidden = true;
      readerEl.innerHTML = "";
      form.submit();
    });
  }

  function startNativeDetectorFallback() {
    if (!("BarcodeDetector" in window)) return;
    var video = readerEl.querySelector("video");
    if (!video) return;

    var detector = null;
    try {
      detector = new window.BarcodeDetector({
        formats: [
          "ean_13",
          "ean_8",
          "code_128",
          "code_39",
          "upc_a",
          "upc_e",
          "itf",
          "qr_code",
        ],
      });
    } catch (e) {
      try {
        detector = new window.BarcodeDetector();
      } catch (e2) {
        return;
      }
    }

    stopNativeLoop();
    nativeTimer = window.setInterval(function () {
      if (handled || !detector || !video || video.readyState < 2) return;
      detector
        .detect(video)
        .then(function (codes) {
          if (handled || !codes || !codes.length) return;
          var raw = codes[0].rawValue || "";
          if (raw) onDetected(raw);
        })
        .catch(function () {
          /* ignore frame errors */
        });
    }, 250);
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
    setStatus("جاري فتح الكاميرا…");

    var formats = undefined;
    try {
      if (typeof Html5QrcodeSupportedFormats !== "undefined") {
        formats = [
          Html5QrcodeSupportedFormats.EAN_13,
          Html5QrcodeSupportedFormats.EAN_8,
          Html5QrcodeSupportedFormats.CODE_128,
          Html5QrcodeSupportedFormats.CODE_39,
          Html5QrcodeSupportedFormats.CODE_93,
          Html5QrcodeSupportedFormats.UPC_A,
          Html5QrcodeSupportedFormats.UPC_E,
          Html5QrcodeSupportedFormats.ITF,
          Html5QrcodeSupportedFormats.CODABAR,
          Html5QrcodeSupportedFormats.QR_CODE,
        ];
      }
    } catch (e) {
      formats = undefined;
    }

    try {
      scanner = formats
        ? new Html5Qrcode("barcode-reader", {
            formatsToSupport: formats,
            verbose: false,
          })
        : new Html5Qrcode("barcode-reader");
    } catch (err) {
      setStatus("تعذر تهيئة قارئ الباركود.", "error");
      return;
    }

    // منطقة مسح عريضة للباركود الشريطي (1D)
    var config = {
      fps: 20,
      qrbox: function (w, h) {
        return {
          width: Math.floor(w * 0.92),
          height: Math.floor(h * 0.28),
        };
      },
      aspectRatio: 1.777,
      disableFlip: false,
      // zxing أوثق لـ EAN من BarcodeDetector في بعض الأجهزة
      experimentalFeatures: {
        useBarCodeDetectorIfSupported: false,
      },
      videoConstraints: {
        facingMode: { ideal: "environment" },
        width: { ideal: 1920 },
        height: { ideal: 1080 },
        focusMode: "continuous",
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
          /* frame miss */
        }
      )
      .then(function () {
        setStatus("قرّب الباركود داخل الإطار الأفقي وثبّت اليد…");
        // مسار إضافي عبر BarcodeDetector إن وُجد
        window.setTimeout(startNativeDetectorFallback, 600);
      })
      .catch(function () {
        // إعادة محاولة بإعدادات أبسط
        return scanner.start(
          { facingMode: "environment" },
          {
            fps: 15,
            qrbox: function (w, h) {
              return {
                width: Math.floor(w * 0.95),
                height: Math.floor(h * 0.35),
              };
            },
            experimentalFeatures: {
              useBarCodeDetectorIfSupported: true,
            },
          },
          function (decodedText) {
            onDetected(decodedText);
          },
          function () {}
        );
      })
      .then(function () {
        if (!handled) {
          setStatus("قرّب الباركود داخل الإطار الأفقي وثبّت اليد…");
          window.setTimeout(startNativeDetectorFallback, 600);
        }
      })
      .catch(function (err) {
        var msg = "تعذر فتح الكاميرا.";
        var name = err && (err.name || err);
        var text = (err && err.message) || String(err || "");
        if (name === "NotAllowedError" || /Permission|NotAllowed/i.test(text)) {
          msg = "تم رفض إذن الكاميرا. اسمح بالوصول من إعدادات المتصفح.";
        } else if (name === "NotFoundError") {
          msg = "لم يتم العثور على كاميرا.";
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
