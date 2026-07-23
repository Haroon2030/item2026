(function () {
  "use strict";

  var btn = document.getElementById("btn-scan");
  var overlay = document.getElementById("barcode-scanner");
  var video = document.getElementById("barcode-scanner-video");
  var statusEl = document.getElementById("barcode-scanner-status");
  var closeBtn = document.getElementById("barcode-scanner-close");
  var input = document.getElementById("q");
  var form = document.querySelector(".search-form");

  if (!btn || !overlay || !video || !statusEl || !input || !form) return;

  var stream = null;
  var detector = null;
  var rafId = 0;
  var scanning = false;
  var handled = false;

  function setStatus(text, kind) {
    statusEl.textContent = text;
    statusEl.classList.remove("is-error", "is-ok");
    if (kind === "error") statusEl.classList.add("is-error");
    if (kind === "ok") statusEl.classList.add("is-ok");
  }

  function stopCamera() {
    scanning = false;
    handled = false;
    if (rafId) {
      window.cancelAnimationFrame(rafId);
      rafId = 0;
    }
    if (stream) {
      stream.getTracks().forEach(function (t) {
        t.stop();
      });
      stream = null;
    }
    video.srcObject = null;
  }

  function closeScanner() {
    stopCamera();
    overlay.hidden = true;
  }

  function onDetected(code) {
    if (handled || !code) return;
    handled = true;
    scanning = false;
    setStatus("تم التقاط: " + code, "ok");
    input.value = code;
    stopCamera();
    overlay.hidden = true;
    form.submit();
  }

  function detectLoop() {
    if (!scanning || !detector || video.readyState < 2) {
      if (scanning) rafId = window.requestAnimationFrame(detectLoop);
      return;
    }

    detector
      .detect(video)
      .then(function (barcodes) {
        if (!scanning || handled) return;
        if (barcodes && barcodes.length) {
          var raw = barcodes[0].rawValue || "";
          if (raw) {
            onDetected(String(raw).trim());
            return;
          }
        }
        rafId = window.requestAnimationFrame(detectLoop);
      })
      .catch(function () {
        if (scanning) rafId = window.requestAnimationFrame(detectLoop);
      });
  }

  function openScanner() {
    if (!window.isSecureContext) {
      setStatus(
        "الكاميرا تحتاج HTTPS. افتح: https://72.61.107.230:8443 ثم اقبل التحذير مرة واحدة.",
        "error"
      );
      overlay.hidden = false;
      return;
    }

    if (!("BarcodeDetector" in window)) {
      overlay.hidden = false;
      setStatus(
        "هذا المتصفح لا يدعم مسح الباركود. استخدم Chrome أو Edge على الجوال.",
        "error"
      );
      return;
    }

    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      overlay.hidden = false;
      setStatus("الكاميرا غير متاحة في هذا المتصفح.", "error");
      return;
    }

    handled = false;
    overlay.hidden = false;
    setStatus("جاري فتح الكاميرا…");

    var formats = [
      "ean_13",
      "ean_8",
      "code_128",
      "code_39",
      "upc_a",
      "upc_e",
      "qr_code",
      "itf",
    ];

    var detectorReady = Promise.resolve();
    try {
      detector = new window.BarcodeDetector({ formats: formats });
    } catch (err) {
      try {
        detector = new window.BarcodeDetector();
      } catch (err2) {
        setStatus("تعذر تهيئة قارئ الباركود.", "error");
        return;
      }
    }

    detectorReady
      .then(function () {
        return navigator.mediaDevices.getUserMedia({
          audio: false,
          video: {
            facingMode: { ideal: "environment" },
            width: { ideal: 1280 },
            height: { ideal: 720 },
          },
        });
      })
      .then(function (mediaStream) {
        stream = mediaStream;
        video.srcObject = stream;
        return video.play();
      })
      .then(function () {
        scanning = true;
        setStatus("وجّه الكاميرا نحو الباركود…");
        rafId = window.requestAnimationFrame(detectLoop);
      })
      .catch(function (err) {
        var msg = "تعذر فتح الكاميرا.";
        if (err && err.name === "NotAllowedError") {
          msg = "تم رفض إذن الكاميرا. اسمح بالوصول ثم أعد المحاولة.";
        } else if (err && err.name === "NotFoundError") {
          msg = "لم يتم العثور على كاميرا.";
        }
        setStatus(msg, "error");
        stopCamera();
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
