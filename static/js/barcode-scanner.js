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

  // قارئ Zebra الحقيقي (لوحة مفاتيح): يكتب ثم Enter
  input.addEventListener("keydown", function (event) {
    if (event.key === "Enter") {
      event.preventDefault();
      if ((input.value || "").trim()) form.submit();
    }
  });

  var controls = null;
  var stream = null;
  var handled = false;
  var pollTimer = null;

  function setStatus(text, kind) {
    statusEl.textContent = text;
    statusEl.classList.remove("is-error", "is-ok");
    if (kind === "error") statusEl.classList.add("is-error");
    if (kind === "ok") statusEl.classList.add("is-ok");
  }

  function beep() {
    try {
      var ctx = new (window.AudioContext || window.webkitAudioContext)();
      var osc = ctx.createOscillator();
      var gain = ctx.createGain();
      osc.type = "square";
      osc.frequency.value = 1800;
      gain.gain.value = 0.05;
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start();
      window.setTimeout(function () {
        osc.stop();
        ctx.close();
      }, 120);
    } catch (e) {}
  }

  function stopAll() {
    handled = false;
    if (pollTimer) {
      window.clearInterval(pollTimer);
      pollTimer = null;
    }
    if (controls && typeof controls.stop === "function") {
      try {
        controls.stop();
      } catch (e) {}
      controls = null;
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
    stopAll();
    overlay.hidden = true;
  }

  function onDetected(code) {
    if (handled || !code) return;
    var value = String(code).trim();
    if (!value) return;
    handled = true;
    beep();
    setStatus("تم المسح: " + value, "ok");
    input.value = value;
    stopAll();
    overlay.hidden = true;
    form.submit();
  }

  function buildHints() {
    if (!window.ZXing) return undefined;
    var hints = new Map();
    hints.set(ZXing.DecodeHintType.POSSIBLE_FORMATS, [
      ZXing.BarcodeFormat.EAN_13,
      ZXing.BarcodeFormat.EAN_8,
      ZXing.BarcodeFormat.CODE_128,
      ZXing.BarcodeFormat.CODE_39,
      ZXing.BarcodeFormat.CODE_93,
      ZXing.BarcodeFormat.UPC_A,
      ZXing.BarcodeFormat.UPC_E,
      ZXing.BarcodeFormat.ITF,
      ZXing.BarcodeFormat.CODABAR,
      ZXing.BarcodeFormat.QR_CODE,
    ]);
    hints.set(ZXing.DecodeHintType.TRY_HARDER, true);
    return hints;
  }

  function startNativePoll() {
    if (!("BarcodeDetector" in window) || pollTimer) return;
    var detector;
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
      return;
    }
    pollTimer = window.setInterval(function () {
      if (handled || video.readyState < 2) return;
      detector
        .detect(video)
        .then(function (codes) {
          if (!handled && codes && codes[0] && codes[0].rawValue) {
            onDetected(codes[0].rawValue);
          }
        })
        .catch(function () {});
    }, 180);
  }

  function openScanner() {
    if (!window.isSecureContext) {
      overlay.hidden = false;
      setStatus("افتح الموقع عبر HTTPS: https://item.alrsheed.net", "error");
      return;
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      overlay.hidden = false;
      setStatus("الكاميرا غير متاحة. ضع المؤشر في حقل البحث واستخدم قارئ Zebra.", "error");
      return;
    }
    if (!window.ZXingBrowser || !window.ZXingBrowser.BrowserMultiFormatReader) {
      overlay.hidden = false;
      setStatus("مكتبة الماسح غير محمّلة. حدّث الصفحة.", "error");
      return;
    }

    handled = false;
    overlay.hidden = false;
    setStatus("جاري تشغيل الماسح بأسلوب Zebra…");

    var hints = buildHints();
    var reader = new window.ZXingBrowser.BrowserMultiFormatReader(hints, 80);

    reader
      .decodeFromConstraints(
        {
          audio: false,
          video: {
            facingMode: { ideal: "environment" },
            width: { ideal: 1920 },
            height: { ideal: 1080 },
            focusMode: "continuous",
          },
        },
        video,
        function (result, err, ctrl) {
          controls = ctrl;
          if (result) onDetected(result.getText());
        }
      )
      .then(function () {
        setStatus("مرّر الباركود على الخط الأحمر مثل جهاز Zebra");
        startNativePoll();
      })
      .catch(function (err) {
        // محاولة ثانية بقيود أبسط
        return reader
          .decodeFromConstraints(
            { audio: false, video: { facingMode: "environment" } },
            video,
            function (result, err2, ctrl) {
              controls = ctrl;
              if (result) onDetected(result.getText());
            }
          )
          .then(function () {
            setStatus("مرّر الباركود على الخط الأحمر مثل جهاز Zebra");
            startNativePoll();
          })
          .catch(function () {
            var text = String((err && (err.name || err.message)) || "");
            var msg = "تعذر فتح الماسح.";
            if (/NotAllowed|Permission/i.test(text)) {
              msg = "اسمح للكاميرا من إعدادات المتصفح.";
            }
            setStatus(msg, "error");
            stopAll();
          });
      });
  }

  btn.addEventListener("click", openScanner);
  if (closeBtn) closeBtn.addEventListener("click", closeScanner);
  overlay.addEventListener("click", function (event) {
    if (event.target === overlay) closeScanner();
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && !overlay.hidden) closeScanner();
  });
})();
