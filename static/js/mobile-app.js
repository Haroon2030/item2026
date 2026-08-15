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

  var PALETTE = [
    "#0e4a6e",
    "#c4a574",
    "#0d6b5c",
    "#8f1d18",
    "#156089",
    "#5c4a32",
    "#3d7a6a",
    "#a67c52",
  ];

  function amt(row, key, displayKey) {
    if (row && row[key] != null && row[key] !== "") {
      var n = Number(row[key]);
      if (isFinite(n)) return n;
    }
    var s = String((row && row[displayKey]) || "").replace(/,/g, "");
    var n2 = Number(s);
    return isFinite(n2) ? n2 : 0;
  }

  function salesOf(row) {
    var a = amt(row, "sales", "sales_display");
    if (a) return a;
    return amt(row, "sales_total", "sales_total_display");
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

  function donut(items, centerLabel) {
    items = (items || []).filter(function (it) {
      return it && it.value > 0;
    });
    if (!items.length) return "";
    var total = 0;
    items.forEach(function (it) {
      total += it.value;
    });
    if (total <= 0) return "";
    var circ = 2 * Math.PI * 34;
    var offset = 0;
    var rings =
      '<circle cx="50" cy="50" r="34" fill="none" stroke="#efe6d8" stroke-width="12"></circle>';
    var legend = "";
    items.forEach(function (it, i) {
      var color = it.color || PALETTE[i % PALETTE.length];
      var len = (it.value / total) * circ;
      rings +=
        '<circle cx="50" cy="50" r="34" fill="none" stroke="' +
        color +
        '" stroke-width="12" stroke-dasharray="' +
        len.toFixed(2) +
        " " +
        circ.toFixed(2) +
        '" stroke-dashoffset="' +
        (-offset).toFixed(2) +
        '" transform="rotate(-90 50 50)"></circle>';
      offset += len;
      legend +=
        '<li><i style="background:' +
        color +
        '"></i><span>' +
        esc(it.label) +
        "</span><b>" +
        esc(it.display || Math.round((it.value / total) * 100) + "%") +
        "</b></li>";
    });
    return (
      '<div class="m-chart"><div class="m-donut-wrap"><svg class="m-donut" viewBox="0 0 100 100" aria-hidden="true">' +
      rings +
      '</svg><div class="m-donut-center"><small>' +
      esc(centerLabel || "") +
      '</small></div></div><ul class="m-legend">' +
      legend +
      "</ul></div>"
    );
  }

  function hbars(rows, limit) {
    var list = (rows || [])
      .filter(function (row) {
        return salesOf(row) > 0;
      })
      .slice();
    list.sort(function (a, b) {
      return salesOf(b) - salesOf(a);
    });
    list = list.slice(0, limit || 8);
    if (!list.length) return "";
    var max = 0;
    list.forEach(function (row) {
      var v = salesOf(row);
      if (v > max) max = v;
    });
    var html = '<div class="m-hbars">';
    list.forEach(function (row) {
      var v = salesOf(row);
      var pct = max > 0 ? Math.max(6, Math.round((v / max) * 100)) : 0;
      html +=
        '<div class="m-hbar"><span>' +
        esc(row.name || row.group_name) +
        '</span><div class="m-hbar-track"><i style="width:' +
        pct +
        '%"></i></div><b>' +
        esc(row.sales_display || row.sales_total_display || "") +
        "</b></div>";
    });
    return html + "</div>";
  }

  function card(title, inner) {
    if (!inner) return "";
    return (
      '<section class="m-card"><div class="m-h">' +
      esc(title) +
      "</div>" +
      inner +
      "</section>"
    );
  }

  function slicesFrom(rows, limit) {
    var list = (rows || [])
      .map(function (row) {
        return {
          label: row.name || row.group_name || "—",
          value: salesOf(row),
          display: row.sales_display || row.sales_total_display || "",
        };
      })
      .filter(function (it) {
        return it.value > 0;
      });
    list.sort(function (a, b) {
      return b.value - a.value;
    });
    var top = list.slice(0, limit || 6);
    var rest = list.slice(limit || 6);
    var other = 0;
    rest.forEach(function (it) {
      other += it.value;
    });
    if (other > 0) {
      top.push({ label: "أخرى", value: other, display: "", color: "#8a7a62" });
    }
    return top;
  }

  function branchTable(rows, totals, withReturns) {
    if (!rows || !rows.length) {
      return '<p class="m-center">لا مبيعات في الفترة.</p>';
    }
    var html =
      '<div class="m-table-wrap"><table class="m-table"><thead><tr><th>#</th><th>الاسم</th><th>المبيعات</th><th>الحصة</th></tr></thead><tbody>';
    rows.forEach(function (row, i) {
      var pct = Math.max(0, Math.min(100, Number(row.share_pct) || 0));
      var meta = esc(row.invoices_display || "0") + " فاتورة";
      if (withReturns) meta += " · مرتجع " + esc(row.returns_display || "0");
      html +=
        "<tr" +
        (row.no_sales ? ' class="dim"' : "") +
        '><td class="idx">' +
        (i + 1) +
        '</td><td class="name"><b>' +
        esc(row.name) +
        "</b><small>" +
        meta +
        '</small></td><td class="amt">' +
        esc(row.sales_display) +
        '</td><td class="share"><span>' +
        esc(row.share_display) +
        '</span><div class="m-bar"><i style="width:' +
        pct +
        '%"></i></div></td></tr>';
    });
    html += "</tbody>";
    if (totals) {
      html +=
        '<tfoot><tr><td></td><td>الإجمالي · ' +
        esc(totals.invoices_display || "0") +
        (withReturns ? " فاتورة · مرتجع " + esc(totals.returns_display || "0") : " فاتورة") +
        '</td><td class="amt">' +
        esc(totals.sales_display || "") +
        "</td><td>100%</td></tr></tfoot>";
    }
    return html + "</table></div>";
  }

  function groupTable(rows, totals) {
    if (!rows || !rows.length) {
      return '<p class="m-center">لا مبيعات مجموعات في الفترة.</p>';
    }
    var html =
      '<div class="m-table-wrap"><table class="m-table"><thead><tr><th>#</th><th>المجموعة</th><th>المبيعات</th><th>الحصة</th></tr></thead><tbody>';
    rows.forEach(function (row, i) {
      var pct = Math.max(0, Math.min(100, Number(row.share_pct) || 0));
      html +=
        '<tr><td class="idx">' +
        (i + 1) +
        '</td><td class="name"><b>' +
        esc(row.name || row.group_name) +
        "</b><small>" +
        esc(row.invoice_count_display || "0") +
        " فاتورة · كمية " +
        esc(row.qty_display || "0") +
        '</small></td><td class="amt">' +
        esc(row.sales_total_display) +
        '</td><td class="share"><span>' +
        esc(row.share_display) +
        '</span><div class="m-bar"><i style="width:' +
        pct +
        '%"></i></div></td></tr>';
    });
    html += "</tbody>";
    if (totals) {
      html +=
        '<tfoot><tr><td></td><td>الإجمالي · ' +
        esc(totals.invoice_count_display || "0") +
        " فاتورة · كمية " +
        esc(totals.qty_display || "") +
        '</td><td class="amt">' +
        esc(totals.sales_total_display || "") +
        "</td><td>100%</td></tr></tfoot>";
    }
    return html + "</table></div>";
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
    html += card(
      "توزيع القنوات",
      donut(
        [
          {
            label: "نقاط البيع",
            value: amt(k, "pos_sales", "pos_sales_display"),
            display: k.pos_sales_display,
            color: "#0e4a6e",
          },
          {
            label: "نظام المبيعات",
            value: amt(k, "wholesale_sales", "wholesale_sales_display"),
            display: k.wholesale_sales_display,
            color: "#c4a574",
          },
          {
            label: "أونكس",
            value: amt(k, "onix_sales", "onix_sales_display"),
            display: k.onix_sales_display,
            color: "#0d6b5c",
          },
        ],
        "القنوات"
      )
    );
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
    html += card(
      "فروع نقاط البيع · " + (d.pos_branches || []).length + " صف",
      branchTable(d.pos_branches, d.pos_totals, true)
    );
    html += card(
      "نظام المبيعات · " + (d.wholesale_branches || []).length + " صف",
      branchTable(d.wholesale_branches, d.wholesale_totals, false)
    );
    html += card("أعلى فروع نقاط البيع", hbars(d.pos_branches, 8));
    return html;
  }

  function groupsView() {
    if (state.loading && !state.groups) {
      return '<div class="m-center"><div class="m-load"></div>جاري التحميل…</div>';
    }
    var g = state.groups;
    if (!g) return '<p class="m-center">لا توجد بيانات لعرضها.</p>';
    var t = g.totals || {};
    var rows = g.rows || [];
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
      "</div>";
    html += card("توزيع المجموعات", donut(slicesFrom(rows, 6), "حصة"));
    html += card(
      "المجموعات · " + rows.length + " صف",
      groupTable(rows, t)
    );
    html += card("أعلى المجموعات", hbars(rows, 8));
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
