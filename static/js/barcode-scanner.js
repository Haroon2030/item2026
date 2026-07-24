(function () {
  "use strict";

  var btn = document.getElementById("btn-scan");
  var overlay = document.getElementById("barcode-scanner");
  var video = document.getElementById("barcode-scanner-video");
  var statusEl = document.getElementById("barcode-scanner-status");
  var closeBtn = document.getElementById("barcode-scanner-close");
  var torchBtn = document.getElementById("barcode-scanner-torch");
  var input = document.getElementById("q");
  var form = document.querySelector(".search-form");

  if (!btn || !overlay || !video || !statusEl || !input || !form) return;

  // قارئ Zebra الحقيقي (لوحة مفاتيح)
  input.addEventListener("keydown", function (event) {
    if (event.key === "Enter") {
      event.preventDefault();
      if ((input.value || "").trim()) form.submit();
    }
  });

  var stream = null;
  var handled = false;
  var running = false;
  var rafId = 0;
  var zxingTimer = 0;
  var nativeDetector = null;
  var zxingReader = null;
  var track = null;
  var torchOn = false;
  var lastCandidate = "";
  var lastCandidateCount = 0;
  var zxingBusy = false;
  var canvas = document.createElement("canvas");
  var ctx = canvas.getContext("2d", { willReadFrequently: true });

  function setStatus(text, kind) {
    statusEl.textContent = text;
    statusEl.classList.remove("is-error", "is-ok");
    if (kind === "error") statusEl.classList.add("is-error");
    if (kind === "ok") statusEl.classList.add("is-ok");
  }

  function beep() {
    try {
      var ctxAudio = new (window.AudioContext || window.webkitAudioContext)();
      var osc = ctxAudio.createOscillator();
      var gain = ctxAudio.createGain();
      osc.type = "square";
      osc.frequency.value = 2100;
      gain.gain.value = 0.06;
      osc.connect(gain);
      gain.connect(ctxAudio.destination);
      osc.start();
      window.setTimeout(function () {
        osc.stop();
        ctxAudio.close();
      }, 90);
    } catch (e) {}
  }

  function vibrate() {
    try {
      if (navigator.vibrate) navigator.vibrate(40);
    } catch (e) {}
  }

  function stopAll() {
    running = false;
    handled = false;
    lastCandidate = "";
    lastCandidateCount = 0;
    zxingBusy = false;
    if (rafId) {
      window.cancelAnimationFrame(rafId);
      rafId = 0;
    }
    if (zxingTimer) {
      window.clearInterval(zxingTimer);
      zxingTimer = 0;
    }
    if (track) {
      try {
        track.applyConstraints({ advanced: [{ torch: false }] });
      } catch (e) {}
      track = null;
    }
    torchOn = false;
    if (torchBtn) {
      torchBtn.hidden = true;
      torchBtn.classList.remove("is-on");
      torchBtn.textContent = "ضوء";
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

  function acceptCode(code) {
    if (handled || !running) return;
    var value = String(code || "").trim();
    if (!value || value.length < 3) return;

    // باركود طويل (EAN/UPC…) يُقبل فوراً؛ القصير يحتاج تأكيداً مزدوجاً
    var needConfirm = value.length < 8 ? 2 : 1;
    if (value === lastCandidate) {
      lastCandidateCount += 1;
    } else {
      lastCandidate = value;
      lastCandidateCount = 1;
    }
    if (lastCandidateCount < needConfirm) return;

    handled = true;
    running = false;
    beep();
    vibrate();
    setStatus("تم المسح: " + value, "ok");
    input.value = value;
    stopAll();
    overlay.hidden = true;
    form.submit();
  }

  function drawScanBand() {
    if (!video.videoWidth || !video.videoHeight || !ctx) return null;
    var vw = video.videoWidth;
    var vh = video.videoHeight;
    // شريط أفقي وسط الصورة = منطقة اللقط الأسرع للباركود الشريطي
    var bandH = Math.max(48, Math.floor(vh * 0.32));
    var y = Math.floor((vh - bandH) / 2);
    canvas.width = vw;
    canvas.height = bandH;
    ctx.drawImage(video, 0, y, vw, bandH, 0, 0, vw, bandH);
    return canvas;
  }

  function buildZxingReader(tryHarder) {
    if (!window.ZXingBrowser || !window.ZXing || !window.ZXingBrowser.BrowserMultiFormatReader) {
      return null;
    }
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
    hints.set(ZXing.DecodeHintType.TRY_HARDER, !!tryHarder);
    // فاصل قصير جداً بين المحاولات
    return new window.ZXingBrowser.BrowserMultiFormatReader(hints, 50);
  }

  function tickNative() {
    if (!running || handled) return;
    rafId = window.requestAnimationFrame(tickNative);
    if (!nativeDetector || video.readyState < 2) return;

    nativeDetector
      .detect(video)
      .then(function (codes) {
        if (!running || handled || !codes || !codes.length) return;
        // فضّل أطول قيمة صالحة (باركود كامل)
        var best = "";
        for (var i = 0; i < codes.length; i++) {
          var raw = (codes[i].rawValue || "").trim();
          if (raw.length > best.length) best = raw;
        }
        if (best) acceptCode(best);
      })
      .catch(function () {});
  }

  function tickZxing() {
    if (!running || handled || zxingBusy || !zxingReader || video.readyState < 2) return;
    var band = drawScanBand();
    if (!band) return;
    zxingBusy = true;
    zxingReader
      .decodeFromCanvas(band)
      .then(function (result) {
        if (result && result.getText) acceptCode(result.getText());
      })
      .catch(function () {})
      .then(function () {
        zxingBusy = false;
      });
  }

  function setupTorchButton() {
    if (!torchBtn || !track) return;
    var caps = {};
    try {
      caps = track.getCapabilities ? track.getCapabilities() : {};
    } catch (e) {
      caps = {};
    }
    if (!caps.torch) {
      torchBtn.hidden = true;
      return;
    }
    torchBtn.hidden = false;
    torchBtn.onclick = function () {
      torchOn = !torchOn;
      track
        .applyConstraints({ advanced: [{ torch: torchOn }] })
        .then(function () {
          torchBtn.classList.toggle("is-on", torchOn);
          torchBtn.textContent = torchOn ? "إيقاف" : "ضوء";
        })
        .catch(function () {
          torchOn = false;
          setStatus("الإضاءة غير متاحة على هذا الجهاز.", "error");
        });
    };
  }

  function openScanner() {
    if (!window.isSecureContext) {
      overlay.hidden = false;
      setStatus("افتح الموقع عبر HTTPS: https://item.alrsheed.net", "error");
      return;
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      overlay.hidden = false;
      setStatus("الكاميرا غير متاحة. استخدم قارئ Zebra على حقل البحث.", "error");
      return;
    }

    stopAll();
    handled = false;
    running = true;
    overlay.hidden = false;
    setStatus("جاري فتح الماسح السريع…");

    // BarcodeDetector أسرع على Chrome/Android إن وُجد
    nativeDetector = null;
    if ("BarcodeDetector" in window) {
      try {
        nativeDetector = new window.BarcodeDetector({
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
          nativeDetector = new window.BarcodeDetector();
        } catch (e2) {
          nativeDetector = null;
        }
      }
    }

    // قارئ سريع أولاً، وإن لزم نبدّل لوضع أدق
    zxingReader = buildZxingReader(false) || buildZxingReader(true);

    var constraints = {
      audio: false,
      video: {
        facingMode: { ideal: "environment" },
        width: { ideal: 1280 },
        height: { ideal: 720 },
        focusMode: "continuous",
        advanced: [{ focusMode: "continuous" }],
      },
    };

    navigator.mediaDevices
      .getUserMedia(constraints)
      .catch(function () {
        return navigator.mediaDevices.getUserMedia({
          audio: false,
          video: { facingMode: "environment" },
        });
      })
      .then(function (mediaStream) {
        stream = mediaStream;
        track = mediaStream.getVideoTracks()[0] || null;
        video.srcObject = stream;
        video.setAttribute("playsinline", "true");
        video.muted = true;
        return video.play();
      })
      .then(function () {
        setupTorchButton();
        setStatus("ثبّت الباركود على الخط الأحمر — المسح سريع ومستمر");
        // مسار أصلي كل إطار
        rafId = window.requestAnimationFrame(tickNative);
        // مسار ZXing على شريط الوسط بسرعة عالية
        if (zxingReader) {
          zxingTimer = window.setInterval(tickZxing, 45);
          // بعد ثانية إن لم يلتقط، فعّل TRY_HARDER (أدق وأبطأ قليلاً)
          window.setTimeout(function () {
            if (!running || handled) return;
            var harder = buildZxingReader(true);
            if (harder) zxingReader = harder;
          }, 900);
        } else if (!nativeDetector) {
          setStatus("مكتبة الماسح غير محمّلة. حدّث الصفحة.", "error");
        }
      })
      .catch(function (err) {
        var text = String((err && (err.name || err.message)) || "");
        var msg = "تعذر فتح الكاميرا.";
        if (/NotAllowed|Permission/i.test(text)) {
          msg = "اسمح للكاميرا من إعدادات المتصفح.";
        } else if (/NotFound/i.test(text)) {
          msg = "لم يتم العثور على كاميرا.";
        }
        setStatus(msg, "error");
        stopAll();
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
