(function () {
  "use strict";

  // لوحات نمو المبيعات (فروع / مجموعات): كل عنصر [data-sg-panel] يُهيَّأ بمعزل عن الآخر.
  var SAR_SVG = "<svg class=\"sar-symbol\" viewBox=\"0 0 1124.14 1256.39\" aria-label=\"ريال سعودي\" role=\"img\" focusable=\"false\"><path fill=\"currentColor\" d=\"M699.62,1113.02h0c-20.06,44.48-33.32,92.75-38.4,143.37l424.51-90.24c20.06-44.47,33.31-92.75,38.4-143.37l-424.51,90.24Z\"/><path fill=\"currentColor\" d=\"M1085.73,895.8c20.06-44.47,33.32-92.75,38.4-143.37l-330.68,70.33v-135.2l292.27-62.11c20.06-44.47,33.32-92.75,38.4-143.37l-330.68,70.27V66.13c-50.67,28.45-95.67,66.32-132.25,110.99v403.35l-123.31,26.15V0c-50.67,28.44-95.67,66.32-132.25,110.99v525.69l-295.91,62.83c-20.06,44.47-33.33,92.75-38.42,143.37l334.33-71.05v170.26l-358.3,76.14c-20.06,44.47-33.32,92.75-38.4,143.37l375.04-79.7c30.53-6.35,56.77-24.4,73.83-50.9l36.68-30.52v92.57l-123.31,26.15v-92.57l36.68,30.52c17.06,26.5,43.3,44.55,73.83,50.9l375.04,79.7Z\"/></svg>";

  // لون ثابت لكل شهر من الأحدث إلى الأقدم؛ كل شهر يُضاف يظهر بخط جديد بلونه
  var COLORS = [
    "#6C9BD1", "#7CC4B0", "#F2C57C", "#E8A0A0", "#A9A1D9", "#4A7FB5",
    "#5BB98C", "#F2B65C", "#E57373", "#8F87C4", "#8FB4DB", "#9AD3C4",
  ];
  var MIN_MONTHS = 2;
  var MAX_MONTHS = 12;

  // إعدادات كل نوع: المجموعات استعلامها أثقل فمهلتها أطول، ولها ترتيب (عدد كبير من المجموعات)
  var KINDS = {
    branch: { store: "sales-growth-months", timeout: 90, sorts: true, defaultSort: "up", noun: "فروع", empty: "لا مبيعات نقاط بيع في هذه الأشهر." },
    group: { store: "sales-group-growth-months", timeout: 360, sorts: true, defaultSort: "sales", noun: "مجموعة", empty: "لا مبيعات مجموعات في هذه الأشهر." },
  };
  var SORTS = [
    { key: "sales", label: "الأكبر مبيعاً" },
    { key: "up", label: "الأعلى نمواً" },
    { key: "down", label: "الأكثر تراجعاً" },
  ];

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function moneyHtml(v) {
    return '<span class="money"><span class="money-value">' + esc(v) + "</span>" + SAR_SVG + "</span>";
  }

  function formatDuration(ms) {
    var sec = Math.max(0, Math.round((Number(ms) || 0) / 1000));
    var m = Math.floor(sec / 60);
    var s = sec % 60;
    if (m <= 0) return s + "ث";
    return s ? m + "د " + s + "ث" : m + "د";
  }

  // التمرير بالعجلة أو بسحب المؤشر لأعلى/لأسفل (نفس سلوك قوائم اللوحات الأخرى)
  function bindDrag(list) {
    if (list.dataset.dragInit === "1") return;
    list.dataset.dragInit = "1";
    var start = null;
    list.addEventListener("pointerdown", function (e) {
      if (e.button !== 0 || e.pointerType === "touch") return;
      start = { y: e.clientY, top: list.scrollTop, moved: false };
    });
    list.addEventListener("pointermove", function (e) {
      if (!start) return;
      var dy = e.clientY - start.y;
      if (!start.moved && Math.abs(dy) < 4) return;
      if (!start.moved) {
        start.moved = true;
        list.classList.add("is-dragging");
        try { list.setPointerCapture(e.pointerId); } catch (err) { /* لا التقاط */ }
      }
      list.scrollTop = start.top - dy;
      e.preventDefault();
    });
    function end() { start = null; list.classList.remove("is-dragging"); }
    list.addEventListener("pointerup", end);
    list.addEventListener("pointercancel", end);
  }

  function setup(panel) {
    var kind = panel.getAttribute("data-sg-kind") === "group" ? "group" : "branch";
    var cfg = KINDS[kind];
    var seed = document.getElementById(panel.getAttribute("data-sg-url-id") || "");
    var baseUrl = "";
    try { baseUrl = seed ? String(JSON.parse(seed.textContent || '""') || "") : ""; } catch (e) { baseUrl = ""; }
    if (!baseUrl) return;

    function el(name) { return panel.querySelector('[data-sg="' + name + '"]'); }
    // مع مجموعة مختارة الاستعلام ثقيل (بنود) فتطول المهلة
    var timeoutSec = baseUrl.indexOf("group=") >= 0 && baseUrl.indexOf("group=&") < 0 && !/group=$/.test(baseUrl) ? 900 : cfg.timeout;
    var state = { months: loadMonths(), sort: cfg.defaultSort, data: null, seq: 0, started: 0 };

    function loadMonths() {
      try {
        var v = parseInt(window.localStorage.getItem(cfg.store), 10);
        if (v >= MIN_MONTHS && v <= MAX_MONTHS) return v;
      } catch (e) { /* التخزين غير متاح */ }
      return 3;
    }

    function saveMonths(n) {
      try { window.localStorage.setItem(cfg.store, String(n)); } catch (e) { /* التخزين غير متاح */ }
    }

    function setStatus(statusKind, label) {
      var s = el("status");
      if (!s) return;
      s.className = "sales-groups-status is-" + (statusKind || "ready");
      s.textContent = label || "";
    }

    function hide(name, on) {
      var n = el(name);
      if (n) n.hidden = !!on;
    }

    function showMessage(msg, isError) {
      var list = el("chart");
      if (list) {
        list.innerHTML =
          '<li class="sales-users-empty' + (isError ? " is-error" : " sales-ov-loading") + '">' + esc(msg) + "</li>";
      }
      hide("total", true);
      hide("head", true);
      var pill = el("pill");
      if (pill) pill.textContent = isError ? "!" : "…";
    }

    // سطر التوضيح + (ترتيب للمجموعات) + اختيار عدد الأشهر
    function renderControls(data) {
      var box = el("legend");
      if (!box) return;
      var btns = "";
      for (var n = MIN_MONTHS; n <= MAX_MONTHS; n++) {
        btns +=
          '<button type="button" class="sg-n' + (n === state.months ? " is-on" : "") + '" data-n="' + n +
          '" aria-pressed="' + (n === state.months ? "true" : "false") + '">' + n + "</button>";
      }
      var sorts = "";
      if (cfg.sorts) {
        sorts = '<div class="sg-sort" role="group" aria-label="ترتيب البطاقات"><span class="sg-count-label">ترتيب</span>';
        SORTS.forEach(function (s) {
          sorts +=
            '<button type="button" class="sg-sort-btn' + (s.key === state.sort ? " is-on" : "") + '" data-sort="' + s.key +
            '" aria-pressed="' + (s.key === state.sort ? "true" : "false") + '">' + s.label + "</button>";
        });
        sorts += "</div>";
      }
      var noun = kind === "group" ? "مجموعة" : "فرع";
      var mode = data.mode || "month";
      state.mode = mode;
      // تسميات الفترات (تاريخ) أطول من اسم الشهر: نوسّع عمودها حتى لا تتداخل مع الأشرطة
      if (mode === "month" || mode === "months") panel.style.removeProperty("--sg-cols");
      else panel.style.setProperty("--sg-cols", "clamp(7.6rem, 24%, 10.5rem) minmax(0, 1fr) 6.2rem 4.4rem");
      var caption;
      var countLabel = "عدد الأشهر";
      if (mode === "months") {
        // عدة أشهر مختارة: كل شهر من الفترة المختارة فقط، بلا سنوات أو فترات سابقة
        caption = "مبيعات كل شهر خلال الفترة المختارة فقط · كل " + noun + " يُقارَن بنفسه (أطول عمود = أعلى شهر)";
        btns = "";
      } else if (mode === "days") {
        // أي فترة مختارة (يوم / عدة أيام / عدة أشهر) تُقارَن بالفترات السابقة بنفس طولها
        var per = data.period || {};
        var len = "";
        if (mode === "days" && per.from && per.to) {
          var d = Math.round((Date.parse(per.to) - Date.parse(per.from)) / 86400000) + 1;
          len = d === 1 ? "يوم واحد" : d + " أيام";
        }
        var curLabel = (data.labels && data.labels[0]) || "";
        caption =
          "الفترة المختارة: " + esc(curLabel) + (len ? " (" + len + ")" : "") + " — " +
          (mode === "days" ? "تُقارَن بنفس التواريخ من الأشهر السابقة" : "تُقارَن بالفترات السابقة لها بنفس عدد الأشهر") +
          " · كل " + noun + " يُقارَن بنفسه (أطول عمود = أعلى فترة)";
        countLabel = mode === "days" ? "عدد الأشهر" : "عدد الفترات";
      } else {
        caption =
          "من 1 إلى " + esc(data.days) + " من كل شهر · كل " + noun + " يُقارَن بنفسه (أطول عمود = أعلى شهر)";
      }
      var countBox = mode === "months" ? "" :
        '<div class="sg-count" role="group" aria-label="' + countLabel + '"><span class="sg-count-label">' + countLabel +
        "</span>" + btns + "</div>";
      box.innerHTML = '<p class="sg-caption">' + caption + "</p>" + sorts + countBox;
      box.hidden = false;
    }

    function lineHtml(m, i, top) {
      var width = m.amount > 0 ? Math.max(2, (m.amount / top) * 100) : 0;
      var pct = m.growth_display
        ? '<bdi dir="ltr">' + esc(m.growth_display) + "</bdi>"
        : '<span class="sg-none">—</span>';
      var tip = m.name + ": " + m.display + (m.growth_display ? " · " + m.growth_display + (state.mode === "month" || !state.mode ? " عن الشهر الذي قبله" : " عن الفترة التي قبلها") : "");
      return (
        '<span class="sg-line is-' + esc(m.direction) + (i === 0 ? " is-current" : "") + '" title="' + esc(tip) + '">' +
        '<span class="sg-month"><i class="sg-sw" style="background:' + COLORS[i] + '"></i>' + esc(m.name) + "</span>" +
        '<span class="sg-track" aria-hidden="true"><span class="sg-fill" style="width:' + width.toFixed(1) + "%;background:" + COLORS[i] + '"></span></span>' +
        '<span class="sg-amt mono">' + moneyHtml(m.display) + "</span>" +
        '<span class="sg-pct mono">' + pct + "</span>" +
        "</span>"
      );
    }

    function linesHtml(months) {
      // كل عنصر يُقارَن بنفسه: أكبر شهر = 100% والبقية نسبةً إليه
      var top = 0.01;
      months.forEach(function (m) { top = Math.max(top, m.amount); });
      return months.map(function (m, i) { return lineHtml(m, i, top); }).join("");
    }

    // شارة النمو في رأس البطاقة: الشهر الحالي عن الذي قبله
    function chipHtml(r, data) {
      var arrow = r.direction === "up" ? "▲" : r.direction === "down" ? "▼" : r.direction === "new" ? "●" : "■";
      var against = (r.months && r.months[1] && r.months[1].name) || data.previous_label || "";
      return (
        '<span class="sg-chip is-' + esc(r.direction) + '">' +
        '<b class="mono"><bdi dir="ltr">' + arrow + " " + esc(r.growth_display) + "</bdi></b>" +
        "<small>" + (state.mode === "days" || state.mode === "months" ? "عن الشهر السابق" : "عن " + esc(against)) + "</small></span>"
      );
    }

    function rowTitle(r) {
      return r.name + (r.code ? " #" + r.code : "") + " — " + r.growth_display + " (" + r.current_display + " مقابل " + r.previous_display + ")";
    }

    function cardHtml(r, data, opts) {
      var name = opts.total
        ? '<span class="sg-name">' + esc(r.name) + "</span>"
        : '<span class="sg-name"><span class="rb-rank mono">' + opts.rank + "</span>" + esc(r.name) + "</span>";
      return (
        '<li class="sg-card is-' + esc(r.direction) + (opts.total ? " is-total" : "") + '"' +
        (opts.total ? "" : ' title="' + esc(rowTitle(r)) + '"') + ">" +
        '<div class="sg-card-head">' + name + chipHtml(r, data) + "</div>" +
        '<span class="sg-lines">' + linesHtml(r.months) + "</span></li>"
      );
    }

    function sortedRows(rows) {
      var out = rows.slice();
      if (state.sort === "sales") {
        out.sort(function (a, b) { return b.current - a.current; });
        return out;
      }
      var g = function (r) { return r.growth_pct == null ? null : r.growth_pct; };
      // «جديد» (بلا شهر سابق) يُؤخَّر في الترتيبين
      var withPct = out.filter(function (r) { return g(r) != null; });
      var noPct = out.filter(function (r) { return g(r) == null; });
      withPct.sort(function (a, b) { return state.sort === "up" ? g(b) - g(a) : g(a) - g(b); });
      return withPct.concat(noPct);
    }

    function render() {
      var data = state.data;
      if (!data) return;
      var rows = data.rows || [];
      var list = el("chart");
      var total = el("total");
      var pill = el("pill");
      var sub = el("sub");

      setStatus("ready", "مكتمل ✓");
      renderControls(data);
      if (pill) pill.textContent = String(rows.length);
      if (sub) {
        sub.textContent =
          "مبيعات نقاط البيع · " + rows.length + " " + (kind === "group" ? "مجموعة" : "فروع") + " · آخر " + data.months +
          " أشهر · خلال " + formatDuration(Date.now() - state.started);
      }
      if (!rows.length) {
        showMessage(cfg.empty, false);
        return;
      }

      var t = data.total || {};
      // أكثر من ستة أشهر: بطاقة الإجمالي طويلة، فتدخل القائمة (في أولها) بدل أن تثبَّت أسفلها
      var inList = data.months > 6;
      var html = inList ? cardHtml(t, data, { total: true }) : "";
      sortedRows(rows).forEach(function (r, i) {
        html += cardHtml(r, data, { rank: i + 1 });
      });
      if (list) {
        list.innerHTML = html;
        list.setAttribute("data-months", String(data.months));
        list.scrollTop = 0;
        bindDrag(list);
      }
      hide("head", false);
      if (total) {
        if (inList) {
          total.hidden = true;
        } else {
          total.innerHTML = cardHtml(t, data, { total: true });
          total.hidden = false;
        }
      }
    }

    function urlFor(n) {
      return baseUrl + (baseUrl.indexOf("?") >= 0 ? "&" : "?") + "months=" + n;
    }

    function load(attempt) {
      var tryNo = attempt || 1;
      var seq = ++state.seq; // يتجاهل رداً قديماً إن غُيّر عدد الأشهر أثناء التحميل
      state.started = Date.now();
      setStatus("loading", "جاري التحميل…");
      showMessage(
        kind === "group"
          ? "جاري حساب المجموعات — أول مرة تستغرق نحو دقيقة ثم تُحفظ…"
          : "جاري تحميل نمو المبيعات…",
        false
      );

      var ctrl = typeof AbortController !== "undefined" ? new AbortController() : null;
      var abortTimer = ctrl ? setTimeout(function () { try { ctrl.abort(); } catch (e) { /* ignore */ } }, timeoutSec * 1000) : null;

      fetch(urlFor(state.months), {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
        cache: "no-store",
        signal: ctrl ? ctrl.signal : undefined,
      })
        .then(function (r) {
          return r.text().then(function (text) {
            var data = null;
            try { data = text ? JSON.parse(text) : null; } catch (e) { data = null; }
            if (!r.ok || (data && data.ok === false)) {
              throw new Error((data && data.error) || (r.ok ? "تعذّر تحميل النمو" : "HTTP " + r.status));
            }
            if (!data || !data.growth) throw new Error("استجابة غير صالحة");
            return data;
          });
        })
        .then(function (payload) {
          if (abortTimer) clearTimeout(abortTimer);
          if (seq !== state.seq) return;
          state.data = payload.growth;
          render();
        })
        .catch(function (err) {
          if (abortTimer) clearTimeout(abortTimer);
          if (seq !== state.seq) return;
          var raw = String((err && err.message) || "");
          var isNet = /failed to fetch|network|انقطع/i.test(raw);
          if (isNet && tryNo < 2) {
            setTimeout(function () { load(tryNo + 1); }, 1500);
            return;
          }
          var msg = raw || "تعذّر تحميل النمو";
          if (err && err.name === "AbortError") msg = "انتهت مهلة التحميل — أوراكل بطيء أو غير مستجيب";
          else if (isNet) msg = "انقطع الاتصال بالسيرفر. حدّث الصفحة (Ctrl+F5)";
          setStatus("error", "غير مكتمل");
          showMessage(msg, true);
        });
    }

    var legend = el("legend");
    if (legend) {
      legend.addEventListener("click", function (e) {
        var target = e.target.closest ? e.target : null;
        var nBtn = target && target.closest(".sg-n");
        if (nBtn) {
          var n = parseInt(nBtn.getAttribute("data-n"), 10);
          if (!(n >= MIN_MONTHS && n <= MAX_MONTHS) || n === state.months) return;
          state.months = n;
          saveMonths(n);
          load(1);
          return;
        }
        var sBtn = target && target.closest(".sg-sort-btn");
        if (sBtn) {
          state.sort = sBtn.getAttribute("data-sort") || cfg.defaultSort;
          render(); // الترتيب محلي: لا طلب جديد
        }
      });
    }
    load(1);
  }

  function init() {
    Array.prototype.forEach.call(document.querySelectorAll("[data-sg-panel]"), setup);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
