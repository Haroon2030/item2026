(function () {
  var TOKEN_KEY = "mobile_token";
  var root = document.getElementById("app");
  var state = {
    token: localStorage.getItem(TOKEN_KEY) || "",
    user: null,
    tab: "daily",
    dateFrom: iso(new Date()),
    dateTo: iso(new Date()),
    branch: "",
    branches: [],
    daily: null,
    groups: null,
    loading: false,
    error: "",
    loginUser: "",
    loginPass: "",
    loginErr: "",
    busy: false,
  };

  function iso(d) {
    var y = d.getFullYear();
    var m = String(d.getMonth() + 1).padStart(2, "0");
    var day = String(d.getDate()).padStart(2, "0");
    return y + "-" + m + "-" + day;
  }

  function esc(v) {
    return String(v == null ? "" : v)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function api(path, opts) {
    opts = opts || {};
    var headers = { Accept: "application/json" };
    if (opts.body) headers["Content-Type"] = "application/json";
    if (state.token) headers.Authorization = "Bearer " + state.token;
    return fetch(path, {
      method: opts.method || "GET",
      headers: headers,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
      cache: "no-store",
    }).then(function (res) {
      return res.text().then(function (text) {
        var data = {};
        try {
          data = text ? JSON.parse(text) : {};
        } catch (e) {
          throw new Error("تعذّر الاتصال بالخادم.");
        }
        if (res.status === 401) {
          throw Object.assign(new Error(data.error || "انتهت الجلسة."), {
            needsLogin: true,
          });
        }
        if (res.status >= 400 || data.ok === false) {
          throw new Error(data.error || "تعذّر تنفيذ الطلب.");
        }
        return data;
      });
    });
  }

  function qs() {
    var q =
      "date_from=" +
      encodeURIComponent(state.dateFrom) +
      "&date_to=" +
      encodeURIComponent(state.dateTo);
    if (state.branch) q += "&branch=" + encodeURIComponent(state.branch);
    return q;
  }

  function logout(remote) {
    var done = function () {
      state.token = "";
      state.user = null;
      state.daily = null;
      state.groups = null;
      localStorage.removeItem(TOKEN_KEY);
      draw();
    };
    if (remote && state.token) {
      api("/api/mobile/logout/", { method: "POST" }).catch(function () {}).then(done);
    } else {
      done();
    }
  }

  function loadAll() {
    state.loading = true;
    state.error = "";
    draw();
    return Promise.all([
      api("/api/mobile/filters/").catch(function () {
        return { branches: [] };
      }),
      api("/api/mobile/sales/daily/?" + qs()),
      api("/api/mobile/sales/groups/?" + qs()),
    ])
      .then(function (parts) {
        state.branches = parts[0].branches || [];
        state.daily = parts[1].daily || null;
        state.groups = parts[2].groups || null;
      })
      .catch(function (err) {
        if (err.needsLogin) {
          logout(false);
          return;
        }
        state.error = err.message || "تعذّر جلب البيانات.";
      })
      .then(function () {
        state.loading = false;
        draw();
      });
  }

  function restore() {
    if (!state.token) {
      draw();
      return;
    }
    state.loading = true;
    draw();
    api("/api/mobile/me/")
      .then(function (data) {
        state.user = data.user || {};
        return loadAll();
      })
      .catch(function (err) {
        if (err.needsLogin) logout(false);
        else {
          state.loading = false;
          state.error = err.message || "تعذّر الاتصال.";
          draw();
        }
      });
  }

  function login(ev) {
    ev.preventDefault();
    state.loginErr = "";
    state.busy = true;
    draw();
    api("/api/mobile/login/", {
      method: "POST",
      body: { username: state.loginUser, password: state.loginPass },
    })
      .then(function (data) {
        state.token = data.token || "";
        state.user = data.user || {};
        localStorage.setItem(TOKEN_KEY, state.token);
        state.busy = false;
        return loadAll();
      })
      .catch(function (err) {
        state.busy = false;
        state.loginErr = err.message || "تعذّر الدخول.";
        draw();
      });
  }

  function kpi(label, value, hint, tone) {
    return (
      '<article class="m-kpi m-kpi-' +
      (tone || "n") +
      '"><span>' +
      esc(label) +
      "</span><b>" +
      esc(value || "0") +
      "</b>" +
      (hint ? "<em>" + esc(hint) + "</em>" : "") +
      "</article>"
    );
  }

  function tiles(rows) {
    if (!rows || !rows.length) {
      return '<p class="m-center">لا مبيعات في الفترة.</p>';
    }
    return rows
      .map(function (row) {
        var pct = Math.max(0, Math.min(100, Number(row.share_pct) || 0));
        return (
          '<article class="m-tile' +
          (row.no_sales ? " dim" : "") +
          '"><div class="m-tile-row"><span>' +
          esc(row.name) +
          "</span><span>" +
          esc(row.sales_display) +
          "</span></div><small>" +
          esc(row.invoices_display) +
          " فاتورة · متوسط سلة " +
          esc(row.avg_basket_display) +
          " · مرتجع " +
          esc(row.returns_display) +
          '</small><div class="m-bar"><i style="width:' +
          pct +
          '%"></i></div><small class="m-share">' +
          esc(row.share_display) +
          "</small></article>"
        );
      })
      .join("");
  }

  function dailyView() {
    if (state.loading && !state.daily) {
      return '<div class="m-center"><div class="m-load"></div>جاري التحميل…</div>';
    }
    if (state.error && !state.daily) {
      return (
        '<p class="m-center">' +
        esc(state.error) +
        '</p><button class="m-btn" id="retry">إعادة المحاولة</button>'
      );
    }
    var d = state.daily;
    if (!d) return '<p class="m-center">لا توجد بيانات لعرضها.</p>';
    var k = d.kpis || {};
    var ranks = d.ranks || {};
    var html = "";
    if (d.from_cache) {
      html += '<div class="m-warn">عرض أرقام محفوظة — تعذّر الاتصال بأوراكل الآن.</div>';
    }
    html +=
      '<section class="m-hero"><span>' +
      esc(d.period_label || "صافي المبيعات") +
      "</span><b>" +
      esc(k.combined_sales_display || k.pos_sales_display || "0") +
      "</b><em>" +
      esc(k.combined_invoices_display || k.pos_invoices_display || "0") +
      " فاتورة عبر القنوات" +
      (d.scope_label ? " · " + esc(d.scope_label) : "") +
      "</em></section>";
    html +=
      '<div class="m-kpis">' +
      kpi("نقاط البيع", k.pos_sales_display, (k.pos_invoices_display || "0") + " فاتورة · " + (k.pos_branches || 0) + " فرع", "pos") +
      kpi("المرتجع", k.pos_returns_display, "", "ret") +
      kpi("نظام المبيعات", k.wholesale_sales_display, (k.wholesale_invoices_display || "0") + " فاتورة", "wh") +
      kpi("أونكس", k.onix_sales_display, "", "onix") +
      "</div>";
    var rankHtml = "";
    [
      ["top_visit_branch", "زيارة"],
      ["top_sales_branch", "مبيعات"],
      ["top_return_branch", "مرتجع"],
    ].forEach(function (pair) {
      var r = ranks[pair[0]];
      if (!r || !r.name || r.name === "—") return;
      rankHtml +=
        '<div class="m-rank"><small>' +
        esc(r.title || pair[1]) +
        "</small><b>" +
        esc(r.name) +
        "</b><span>" +
        esc(r.value_display) +
        (r.hint ? " · " + esc(r.hint) : "") +
        "</span></div>";
    });
    if (rankHtml) html += '<div class="m-ranks">' + rankHtml + "</div>";
    html += '<div class="m-h">فروع نقاط البيع</div>' + tiles(d.pos_branches);
    html += '<div class="m-h">نظام المبيعات</div>' + tiles(d.wholesale_branches);
    return html;
  }

  function groupsView() {
    if (state.loading && !state.groups) {
      return '<div class="m-center"><div class="m-load"></div>جاري التحميل…</div>';
    }
    var g = state.groups;
    if (!g) return '<p class="m-center">لا توجد بيانات لعرضها.</p>';
    var t = g.totals || {};
    var html = "";
    if (g.warning) html += '<div class="m-warn">' + esc(g.warning) + "</div>";
    html +=
      '<section class="m-hero"><span>مبيعات المجموعات</span><b>' +
      esc(t.sales_total_display || "0") +
      "</b><em>" +
      esc(t.group_count_display || "0") +
      " مجموعة · كمية " +
      esc(t.qty_display || "0") +
      "</em></section>";
    html +=
      '<div class="m-kpis">' +
      kpi("الفواتير", t.invoice_count_display, "", "pos") +
      kpi("الكمية", t.qty_display, "", "onix") +
      '</div><div class="m-h">توزيع المجموعات</div>';
    var rows = g.rows || [];
    if (!rows.length) html += '<p class="m-center">لا مبيعات مجموعات في الفترة.</p>';
    rows.forEach(function (row) {
      var pct = Math.max(0, Math.min(100, Number(row.share_pct) || 0));
      html +=
        '<article class="m-tile"><div class="m-tile-row"><span>' +
        esc(row.name || row.group_name) +
        "</span><span>" +
        esc(row.sales_total_display) +
        "</span></div><small>" +
        esc(row.invoice_count_display) +
        " فاتورة · كمية " +
        esc(row.qty_display) +
        '</small><div class="m-bar"><i style="width:' +
        pct +
        '%"></i></div><small class="m-share">' +
        esc(row.share_display) +
        "</small></article>";
    });
    return html;
  }

  function loginView() {
    return (
      '<div class="m-login"><div class="m-brand"><div class="m-mark" aria-hidden="true"><svg viewBox="0 0 24 24"><rect x="3.4" y="13.2" width="3.2" height="7.2" rx="1.6" fill="#c4a574"/><rect x="8.1" y="10.2" width="3.2" height="10.2" rx="1.6" fill="#e8d5b0"/><rect x="12.8" y="7.6" width="3.2" height="12.8" rx="1.6" fill="#fff"/><rect x="17.5" y="4.4" width="3.2" height="16" rx="1.6" fill="#c4a574"/></svg></div><h1>مبيعات الرشيد</h1><p>دفتر المبيعات اليومية والمجموعات</p></div>' +
      '<form class="m-login-card" id="login-form">' +
      '<label class="m-field"><span>المستخدم أو الرقم</span><input id="u" autocomplete="username" value="' +
      esc(state.loginUser) +
      '"></label>' +
      '<label class="m-field"><span>كلمة المرور</span><input id="p" type="password" autocomplete="current-password"></label>' +
      (state.loginErr ? '<p class="m-err">' + esc(state.loginErr) + "</p>" : "") +
      '<button class="m-btn" type="submit"' +
      (state.busy ? " disabled" : "") +
      ">" +
      (state.busy ? "جاري الدخول…" : "دخول") +
      "</button></form></div>"
    );
  }

  function shellView() {
    var name = (state.user && (state.user.display_name || state.user.username)) || "مبيعات الرشيد";
    var role = (state.user && state.user.role_name) || "";
    var branchOpts = '<option value="">كل الفروع</option>';
    (state.branches || []).forEach(function (b) {
      branchOpts +=
        '<option value="' +
        esc(b.code) +
        '"' +
        (state.branch === b.code ? " selected" : "") +
        ">" +
        esc(b.name) +
        "</option>";
    });
    return (
      '<div class="m-shell-head"><div class="m-head-row"><div class="m-head-mark" aria-hidden="true"><svg viewBox="0 0 24 24"><rect x="3.4" y="13.2" width="3.2" height="7.2" rx="1.6" fill="#c4a574"/><rect x="8.1" y="10.2" width="3.2" height="10.2" rx="1.6" fill="#e8d5b0"/><rect x="12.8" y="7.6" width="3.2" height="12.8" rx="1.6" fill="#fff"/><rect x="17.5" y="4.4" width="3.2" height="16" rx="1.6" fill="#c4a574"/></svg></div><div class="m-head-copy"><h2>' +
      esc(name) +
      "</h2><small>" +
      esc(role || "مبيعات الرشيد") +
      '</small></div></div><div class="m-actions"><button type="button" id="today">اليوم</button><button type="button" id="out">خروج</button></div>' +
      '<div class="m-filters"><input id="df" type="date" value="' +
      esc(state.dateFrom) +
      '"><input id="dt" type="date" value="' +
      esc(state.dateTo) +
      '"><select id="br">' +
      branchOpts +
      '</select></div></div><div class="m-page" id="page"></div>' +
      '<nav class="m-nav"><button type="button" id="tab-daily" class="' +
      (state.tab === "daily" ? "on" : "") +
      '"><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M4 18.2h16v-1.7H4v1.7zm0-5.7h11.2V10.8H4v1.7zM4 5.8v1.7h16V5.8H4z"/></svg><span>اليوم</span></button><button type="button" id="tab-groups" class="' +
      (state.tab === "groups" ? "on" : "") +
      '"><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M4.2 5.2h6.4v6.4H4.2V5.2zm9.2 0h6.4v3.8h-6.4V5.2zM4.2 13.4h6.4v5.4H4.2v-5.4zm9.2-2.6h6.4v8h-6.4v-8z"/></svg><span>المجموعات</span></button></nav>'
    );
  }

  function bind() {
    var form = document.getElementById("login-form");
    if (form) {
      document.getElementById("u").addEventListener("input", function (e) {
        state.loginUser = e.target.value;
      });
      document.getElementById("p").addEventListener("input", function (e) {
        state.loginPass = e.target.value;
      });
      form.addEventListener("submit", login);
      return;
    }
    var retry = document.getElementById("retry");
    if (retry) retry.addEventListener("click", loadAll);
    var today = document.getElementById("today");
    if (today) {
      today.addEventListener("click", function () {
        state.dateFrom = iso(new Date());
        state.dateTo = iso(new Date());
        loadAll();
      });
    }
    var out = document.getElementById("out");
    if (out) out.addEventListener("click", function () { logout(true); });
    var df = document.getElementById("df");
    var dt = document.getElementById("dt");
    var br = document.getElementById("br");
    function applyRange() {
      state.dateFrom = df.value || state.dateFrom;
      state.dateTo = dt.value || state.dateTo;
      state.branch = br.value || "";
      loadAll();
    }
    if (df) df.addEventListener("change", applyRange);
    if (dt) dt.addEventListener("change", applyRange);
    if (br) br.addEventListener("change", applyRange);
    var tabDaily = document.getElementById("tab-daily");
    var tabGroups = document.getElementById("tab-groups");
    if (tabDaily) {
      tabDaily.addEventListener("click", function () {
        state.tab = "daily";
        draw();
      });
    }
    if (tabGroups) {
      tabGroups.addEventListener("click", function () {
        state.tab = "groups";
        draw();
      });
    }
  }

  function draw() {
    if (!state.token) {
      root.innerHTML = loginView();
      bind();
      return;
    }
    root.innerHTML = shellView();
    var page = document.getElementById("page");
    if (page) page.innerHTML = state.tab === "groups" ? groupsView() : dailyView();
    bind();
  }

  restore();
})();
