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
  var zxingControls = null;
  var track = null;
  var torchOn = false;
  var lastCandidate = "";
  var lastCandidateCount = 0;
  var zxingBusy = false;
  var passIndex = 0;
  var audioCtx = null;
  var canvas = document.createElement("canvas");
  var ctx = canvas.getContext("2d", { willReadFrequently: true, alpha: false });

  // مربع كبير: إطار كامل تقريباً ثم شريط وسط (بدون تباين عنيف)
  var SCAN_PASSES = [
    { y: 0.5, h: 0.82, scale: 1.0, contrast: 1.0, xPad: 0.04 },
    { y: 0.5, h: 0.45, scale: 1.35, contrast: 1.1, xPad: 0.02 },
    { y: 0.5, h: 0.28, scale: 1.7, contrast: 1.15, xPad: 0.02 },
  ];

  function setStatus(text, kind) {
    statusEl.textContent = text;
    statusEl.classList.remove("is-error", "is-ok");
    if (kind === "error") statusEl.classList.add("is-error");
    if (kind === "ok") statusEl.classList.add("is-ok");
  }

  function ensureAudio() {
    try {
      var AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return null;
      if (!audioCtx || audioCtx.state === "closed") {
        audioCtx = new AC();
      }
      if (audioCtx.state === "suspended") {
        audioCtx.resume().catch(function () {});
      }
      return audioCtx;
    } catch (e) {
      return null;
    }
  }

  function tone(ctxAudio, freq, startAt, dur, vol) {
    var osc = ctxAudio.createOscillator();
    var gain = ctxAudio.createGain();
    osc.type = "square";
    osc.frequency.value = freq;
    gain.gain.setValueAtTime(0.0001, startAt);
    gain.gain.exponentialRampToValueAtTime(vol, startAt + 0.01);
    gain.gain.exponentialRampToValueAtTime(0.0001, startAt + dur);
    osc.connect(gain);
    gain.connect(ctxAudio.destination);
    osc.start(startAt);
    osc.stop(startAt + dur + 0.02);
  }

  function beep(freq, ms) {
    var ctxAudio = ensureAudio();
    if (!ctxAudio) return;
    try {
      var now = ctxAudio.currentTime;
      tone(ctxAudio, freq || 1800, now, (ms || 70) / 1000, 0.18);
    } catch (e) {}
  }

  function playScanSuccess() {
    var ctxAudio = ensureAudio();
    if (!ctxAudio) return;
    try {
      // نغمتان قصيرتان بأسلوب قارئات Honeywell/Zebra
      var now = ctxAudio.currentTime;
      tone(ctxAudio, 1800, now, 0.08, 0.22);
      tone(ctxAudio, 2400, now + 0.09, 0.11, 0.22);
    } catch (e) {}
  }

  function vibrate() {
    try {
      if (navigator.vibrate) navigator.vibrate([40, 25, 50]);
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
    if (zxingControls && typeof zxingControls.stop === "function") {
      try {
        zxingControls.stop();
      } catch (e) {}
      zxingControls = null;
    }
    if (zxingReader && typeof zxingReader.reset === "function") {
      try {
        zxingReader.reset();
      } catch (e2) {}
    }
    zxingReader = null;
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
    // يشمل رموز Codabar/GS1 الخاصة: $ + : %
    if (!/^[0-9A-Za-z\-_./$+:%]+$/.test(value)) return;

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
    playScanSuccess();
    vibrate();
    setStatus("تم المسح: " + value, "ok");
    input.value = value;
    stopAll();
    overlay.hidden = true;
    // انتظر قليلاً حتى يكتمل صوت النجاح قبل إرسال النموذج
    window.setTimeout(function () {
      form.submit();
    }, 180);
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
    var bandH = Math.max(96, Math.floor(vh * pass.h));
    var y = Math.floor(vh * pass.y - bandH / 2);
    if (y < 0) y = 0;
    if (y + bandH > vh) y = vh - bandH;

    var xPad = typeof pass.xPad === "number" ? pass.xPad : 0.03;
    var x = Math.floor(vw * xPad);
    var bandW = Math.max(240, Math.floor(vw * (1 - xPad * 2)));
    if (x + bandW > vw) bandW = vw - x;

    var outW = Math.max(320, Math.floor(bandW * pass.scale));
    var outH = Math.max(64, Math.floor(bandH * pass.scale));
    // حدّ أقصى حتى لا يتجمّد الهاتف
    if (outW > 1400) {
      var ratio = 1400 / outW;
      outW = 1400;
      outH = Math.max(64, Math.floor(outH * ratio));
    }

    canvas.width = outW;
    canvas.height = outH;
    ctx.imageSmoothingEnabled = pass.scale > 1.15;
    ctx.drawImage(video, x, y, bandW, bandH, 0, 0, outW, outH);

    try {
      if (pass.contrast && pass.contrast > 1.05) {
        var img = ctx.getImageData(0, 0, outW, outH);
        ctx.putImageData(boostContrast(img, pass.contrast), 0, 0);
      }
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
      // 1D
      ZXing.BarcodeFormat.EAN_13,
      ZXing.BarcodeFormat.EAN_8,
      ZXing.BarcodeFormat.CODE_128,
      ZXing.BarcodeFormat.CODE_39,
      ZXing.BarcodeFormat.CODE_93,
      ZXing.BarcodeFormat.UPC_A,
      ZXing.BarcodeFormat.UPC_E,
      ZXing.BarcodeFormat.UPC_EAN_EXTENSION,
      ZXing.BarcodeFormat.ITF, // Interleaved 2 of 5
      ZXing.BarcodeFormat.CODABAR,
      ZXing.BarcodeFormat.RSS_14, // GS1 DataBar
      ZXing.BarcodeFormat.RSS_EXPANDED, // GS1 DataBar Expanded
      // 2D
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
    // تقريب خفيف قد يشوّش الباركود على بعض الأجهزة — نترك التركيز فقط
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
      return scored[0] && scored[0].score > 0 ? scored[0].id : null;
    });
  }

  function openCameraStream() {
    return pickBackCameraId().then(function (deviceId) {
      var attempts = [];
      if (deviceId) {
        attempts.push({
          audio: false,
          video: {
            deviceId: { ideal: deviceId },
            width: { ideal: 1280 },
            height: { ideal: 720 },
            facingMode: { ideal: "environment" },
          },
        });
      }
      attempts.push({
        audio: false,
        video: {
          facingMode: { ideal: "environment" },
          width: { ideal: 1280 },
          height: { ideal: 720 },
        },
      });
      attempts.push({ audio: false, video: { facingMode: "environment" } });
      attempts.push({ audio: false, video: true });

      function next(i) {
        if (i >= attempts.length) {
          return Promise.reject(new Error("NoCamera"));
        }
        return navigator.mediaDevices.getUserMedia(attempts[i]).catch(function () {
          return next(i + 1);
        });
      }
      return next(0);
    });
  }

  function startZxingContinuous() {
    zxingReader = buildZxingReader(true) || buildZxingReader(false);
    if (!zxingReader || !stream) return Promise.resolve(null);
    // المسار الرسمي المستمر — أدق من قصّ canvas يدوياً
    return zxingReader
      .decodeFromStream(stream, video, function (result) {
        if (result && result.getText) acceptCode(result.getText());
      })
      .then(function (controls) {
        zxingControls = controls || null;
        return controls;
      })
      .catch(function () {
        // احتياطي: فك من canvas كل 50ms
        zxingTimer = window.setInterval(tickZxing, 50);
        return null;
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
    ensureAudio(); // تفعيل الصوت من لمسة المستخدم (مطلوب في الموبايل)

    nativeDetector = null;
    if ("BarcodeDetector" in window) {
      try {
        nativeDetector = new window.BarcodeDetector({
          formats: [
            "ean_13",
            "ean_8",
            "code_128",
            "code_39",
            "code_93",
            "upc_a",
            "upc_e",
            "itf",
            "codabar",
            "databar",
            "databar_expanded",
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

    zxingReader = null;

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
        setStatus("ضع الباركود داخل المربع الكبير — جاري القراءة…");
        rafId = window.requestAnimationFrame(tickNative);
        return startZxingContinuous();
      })
      .then(function () {
        if (!zxingReader && !nativeDetector && !zxingTimer) {
          setStatus("مكتبة الماسح غير محمّلة. حدّث الصفحة.", "error");
        }
      })
      .catch(function (err) {
        var text = String((err && (err.name || err.message)) || "");
        var msg = "تعذر فتح الكاميرا.";
        if (/NotAllowed|Permission/i.test(text)) {
          msg = "اسمح للكاميرا من إعدادات المتصفح.";
        } else if (/NotFound|NoCamera/i.test(text)) {
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
