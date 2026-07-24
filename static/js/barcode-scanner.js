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

  /* =========================================================
   * 1) أجهزة Honeywell / Zebra الحقيقية (Keyboard Wedge)
   * ========================================================= */
  var lastKeyAt = 0;
  var wedgeChars = 0;

  function focusSearch() {
    try {
      input.focus({ preventScroll: true });
    } catch (e) {
      input.focus();
    }
    try {
      input.select();
    } catch (e2) {}
  }

  // إبقاء الحقل جاهزاً للقارئ الخارجي
  window.setTimeout(focusSearch, 120);
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden && overlay.hidden) focusSearch();
  });

  input.addEventListener("keydown", function (event) {
    var now = Date.now();
    var gap = now - lastKeyAt;
    lastKeyAt = now;

    if (event.key === "Enter") {
      event.preventDefault();
      var value = (input.value || "").trim();
      if (!value) return;
      // نغمة قصيرة مثل أجهزة القراءة عند نجاح الـ wedge
      if (wedgeChars >= 4 || gap < 80) beep(1800, 70);
      form.submit();
      wedgeChars = 0;
      return;
    }

    if (event.key.length === 1 && !event.ctrlKey && !event.altKey && !event.metaKey) {
      // فجوات قصيرة جداً = قارئ خارجي يكتب بسرعة
      if (gap > 0 && gap < 45) {
        wedgeChars += 1;
      } else if (gap > 120) {
        wedgeChars = 1;
      }
    }
  });

  /* =========================================================
   * 2) ماسح الكاميرا (أقرب ما يمكن لأسلوب Zebra)
   * ========================================================= */
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
  var passIndex = 0;
  var canvas = document.createElement("canvas");
  var ctx = canvas.getContext("2d", { willReadFrequently: true, alpha: false });

  // مناطق مسح متعددة (وسط / أعلى / أسفل) + تكبير للباركود الصغير
  var SCAN_PASSES = [
    { y: 0.5, h: 0.26, scale: 1.0, contrast: 1.45 },
    { y: 0.5, h: 0.2, scale: 1.85, contrast: 1.7 },
    { y: 0.42, h: 0.22, scale: 1.35, contrast: 1.55 },
    { y: 0.58, h: 0.22, scale: 1.35, contrast: 1.55 },
    { y: 0.5, h: 0.36, scale: 1.15, contrast: 1.35 },
  ];

  function setStatus(text, kind) {
    statusEl.textContent = text;
    statusEl.classList.remove("is-error", "is-ok");
    if (kind === "error") statusEl.classList.add("is-error");
    if (kind === "ok") statusEl.classList.add("is-ok");
  }

  function beep(freq, ms) {
    try {
      var ctxAudio = new (window.AudioContext || window.webkitAudioContext)();
      var osc = ctxAudio.createOscillator();
      var gain = ctxAudio.createGain();
      osc.type = "square";
      osc.frequency.value = freq || 2200;
      gain.gain.value = 0.07;
      osc.connect(gain);
      gain.connect(ctxAudio.destination);
      osc.start();
      window.setTimeout(function () {
        osc.stop();
        ctxAudio.close();
      }, ms || 85);
    } catch (e) {}
  }

  function vibrate() {
    try {
      if (navigator.vibrate) navigator.vibrate([35, 20, 35]);
    } catch (e) {}
  }

  function stopAll() {
    running = false;
    handled = false;
    lastCandidate = "";
    lastCandidateCount = 0;
    zxingBusy = false;
    passIndex = 0;
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
    focusSearch();
  }

  function normalizeCode(code) {
    return String(code || "")
      .replace(/[\u200e\u200f\u202a-\u202e]/g, "")
      .trim();
  }

  function acceptCode(code) {
    if (handled || !running) return;
    var value = normalizeCode(code);
    if (!value || value.length < 3) return;
    // تجاهل قراءات غير منطقية قصيرة جداً من الضوضاء
    if (!/^[0-9A-Za-z\-_./]+$/.test(value)) return;

    // EAN/UPC/Code128 الطويل: قبول فوري. القصير: تأكيد مزدوج
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
    beep(2300, 90);
    vibrate();
    setStatus("تم المسح: " + value, "ok");
    input.value = value;
    stopAll();
    overlay.hidden = true;
    form.submit();
  }

  function boostContrast(imageData, contrast) {
    var d = imageData.data;
    var c = contrast || 1.5;
    var intercept = 128 * (1 - c);
    for (var i = 0; i < d.length; i += 4) {
      var g = 0.299 * d[i] + 0.587 * d[i + 1] + 0.114 * d[i + 2];
      g = c * g + intercept;
      if (g < 0) g = 0;
      else if (g > 255) g = 255;
      d[i] = d[i + 1] = d[i + 2] = g;
    }
    return imageData;
  }

  function drawPass(pass) {
    if (!video.videoWidth || !video.videoHeight || !ctx) return null;
    var vw = video.videoWidth;
    var vh = video.videoHeight;
    var bandH = Math.max(56, Math.floor(vh * pass.h));
    var y = Math.floor(vh * pass.y - bandH / 2);
    if (y < 0) y = 0;
    if (y + bandH > vh) y = vh - bandH;

    var outW = Math.max(320, Math.floor(vw * pass.scale));
    var outH = Math.max(48, Math.floor(bandH * pass.scale));
    // حدّ أقصى حتى لا يتجمّد الهاتف
    if (outW > 1600) {
      var ratio = 1600 / outW;
      outW = 1600;
      outH = Math.max(48, Math.floor(outH * ratio));
    }

    canvas.width = outW;
    canvas.height = outH;
    ctx.imageSmoothingEnabled = pass.scale > 1.2;
    ctx.drawImage(video, 0, y, vw, bandH, 0, 0, outW, outH);

    try {
      var img = ctx.getImageData(0, 0, outW, outH);
      ctx.putImageData(boostContrast(img, pass.contrast), 0, 0);
    } catch (e) {
      // بعض المتصفحات تمنع getImageData في حالات نادرة
    }
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
      ZXing.BarcodeFormat.DATA_MATRIX,
    ]);
    hints.set(ZXing.DecodeHintType.TRY_HARDER, !!tryHarder);
    if (ZXing.DecodeHintType.ASSUME_GS1 != null) {
      hints.set(ZXing.DecodeHintType.ASSUME_GS1, false);
    }
    return new window.ZXingBrowser.BrowserMultiFormatReader(hints, 30);
  }

  function tickNative() {
    if (!running || handled) return;
    rafId = window.requestAnimationFrame(tickNative);
    if (!nativeDetector || video.readyState < 2) return;

    nativeDetector
      .detect(video)
      .then(function (codes) {
        if (!running || handled || !codes || !codes.length) return;
        var best = "";
        for (var i = 0; i < codes.length; i++) {
          var raw = normalizeCode(codes[i].rawValue);
          if (raw.length > best.length) best = raw;
        }
        if (best) acceptCode(best);
      })
      .catch(function () {});
  }

  function tickZxing() {
    if (!running || handled || zxingBusy || !zxingReader || video.readyState < 2) return;
    var pass = SCAN_PASSES[passIndex % SCAN_PASSES.length];
    passIndex += 1;
    var band = drawPass(pass);
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

  function tuneCameraTrack() {
    if (!track || !track.getCapabilities) return;
    var caps = {};
    try {
      caps = track.getCapabilities() || {};
    } catch (e) {
      return;
    }
    var advanced = [];
    if (caps.focusMode && caps.focusMode.indexOf("continuous") !== -1) {
      advanced.push({ focusMode: "continuous" });
    }
    if (caps.exposureMode && caps.exposureMode.indexOf("continuous") !== -1) {
      advanced.push({ exposureMode: "continuous" });
    }
    if (caps.whiteBalanceMode && caps.whiteBalanceMode.indexOf("continuous") !== -1) {
      advanced.push({ whiteBalanceMode: "continuous" });
    }
    // تقريب خفيف يملأ الشريط مثل ماسح Zebra
    if (caps.zoom) {
      var zMin = caps.zoom.min || 1;
      var zMax = caps.zoom.max || 1;
      var z = zMin + (zMax - zMin) * 0.18;
      advanced.push({ zoom: z });
    }
    if (!advanced.length) return;
    track.applyConstraints({ advanced: advanced }).catch(function () {});
  }

  function pickBackCameraId() {
    if (!navigator.mediaDevices.enumerateDevices) {
      return Promise.resolve(null);
    }
    return navigator.mediaDevices.enumerateDevices().then(function (devices) {
      var videos = devices.filter(function (d) {
        return d.kind === "videoinput";
      });
      if (!videos.length) return null;
      var scored = videos
        .map(function (d) {
          var label = (d.label || "").toLowerCase();
          var score = 0;
          if (/back|rear|environment|خلف|خلفية|خلفى/.test(label)) score += 10;
          if (/wide|ultra/.test(label)) score += 2;
          if (/front|user|أمام|امام/.test(label)) score -= 10;
          return { id: d.deviceId, score: score, label: label };
        })
        .sort(function (a, b) {
          return b.score - a.score;
        });
      return scored[0] && scored[0].score >= 0 ? scored[0].id : scored[0].id;
    });
  }

  function openCameraStream() {
    return pickBackCameraId().then(function (deviceId) {
      var baseVideo = {
        width: { ideal: 1920, min: 640 },
        height: { ideal: 1080, min: 480 },
        frameRate: { ideal: 30, min: 15 },
        focusMode: "continuous",
      };
      if (deviceId) {
        baseVideo.deviceId = { exact: deviceId };
      } else {
        baseVideo.facingMode = { ideal: "environment" };
      }

      return navigator.mediaDevices
        .getUserMedia({ audio: false, video: baseVideo })
        .catch(function () {
          return navigator.mediaDevices.getUserMedia({
            audio: false,
            video: {
              facingMode: { ideal: "environment" },
              width: { ideal: 1280 },
              height: { ideal: 720 },
            },
          });
        })
        .catch(function () {
          return navigator.mediaDevices.getUserMedia({
            audio: false,
            video: { facingMode: "environment" },
          });
        });
    });
  }

  function openScanner() {
    if (!window.isSecureContext) {
      overlay.hidden = false;
      setStatus("افتح الموقع عبر HTTPS: https://item.alrsheed.net", "error");
      return;
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      overlay.hidden = false;
      setStatus("الكاميرا غير متاحة. استخدم قارئ Honeywell/Zebra على حقل البحث.", "error");
      return;
    }

    stopAll();
    handled = false;
    running = true;
    overlay.hidden = false;
    setStatus("جاري تشغيل الماسح الاحترافي…");

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
            "codabar",
            "qr_code",
            "data_matrix",
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

    zxingReader = buildZxingReader(false) || buildZxingReader(true);

    openCameraStream()
      .then(function (mediaStream) {
        stream = mediaStream;
        track = mediaStream.getVideoTracks()[0] || null;
        video.srcObject = stream;
        video.setAttribute("playsinline", "true");
        video.setAttribute("autoplay", "true");
        video.muted = true;
        tuneCameraTrack();
        return video.play();
      })
      .then(function () {
        setupTorchButton();
        setStatus("مرّر الباركود على الخط الأحمر — مثل جهاز Zebra");
        rafId = window.requestAnimationFrame(tickNative);
        if (zxingReader) {
          zxingTimer = window.setInterval(tickZxing, 35);
          window.setTimeout(function () {
            if (!running || handled) return;
            var harder = buildZxingReader(true);
            if (harder) zxingReader = harder;
            setStatus("وضع دقة أعلى — قرّب الباركود وثبّت اليد");
          }, 1200);
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
