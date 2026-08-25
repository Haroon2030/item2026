/**
 * تحديث إجباري:
 * 1) بعد كل نشر (تغيّر APP_CLIENT_VERSION)
 * 2) مرة واحدة بعد تسجيل الدخول — مسح كاش ثم إعادة تحميل (مثل Ctrl+Shift+R)
 */
(function () {
  "use strict";

  var KEY = "app-client-ver";
  var HARD_DONE = "app-hard-refresh-done";
  var htmlVer = document.documentElement.getAttribute("data-app-ver") || "";
  var needHard =
    document.documentElement.getAttribute("data-hard-refresh") === "1";

  function flagKey(ver) {
    return "app-force-reload:" + ver;
  }

  function stripParams() {
    try {
      var u = new URL(location.href);
      var dirty = false;
      ["_cv", "_hr"].forEach(function (k) {
        if (u.searchParams.has(k)) {
          u.searchParams.delete(k);
          dirty = true;
        }
      });
      if (!dirty) return;
      var next = u.pathname + (u.search || "") + u.hash;
      history.replaceState(null, "", next || "/");
    } catch (e) {}
  }

  function clearSiteCaches() {
    var jobs = [];
    if (window.caches && caches.keys) {
      jobs.push(
        caches.keys().then(function (keys) {
          return Promise.all(keys.map(function (k) { return caches.delete(k); }));
        })
      );
    }
    if (navigator.serviceWorker && navigator.serviceWorker.getRegistrations) {
      jobs.push(
        navigator.serviceWorker.getRegistrations().then(function (regs) {
          return Promise.all(regs.map(function (r) { return r.unregister(); }));
        })
      );
    }
    return jobs.length ? Promise.all(jobs) : Promise.resolve();
  }

  function reloadBusted(param, value) {
    var u;
    try {
      u = new URL(location.href);
      u.searchParams.set(param, value);
    } catch (e) {
      location.reload();
      return;
    }
    clearSiteCaches().then(
      function () { location.replace(u.href); },
      function () { location.replace(u.href); }
    );
  }

  function hardRefreshAfterLogin() {
    try {
      if (sessionStorage.getItem(HARD_DONE) === "1") {
        sessionStorage.removeItem(HARD_DONE);
        stripParams();
        return false;
      }
    } catch (e) {}

    if (!needHard) return false;

    try {
      sessionStorage.setItem(HARD_DONE, "1");
      localStorage.removeItem(KEY);
    } catch (e) {}

    reloadBusted("_hr", String(Date.now()));
    return true;
  }

  function applyVersion(ver) {
    ver = String(ver || "").trim();
    if (!ver) return false;
    try {
      if (sessionStorage.getItem(flagKey(ver)) === "1") {
        localStorage.setItem(KEY, ver);
        sessionStorage.removeItem(flagKey(ver));
        stripParams();
        return false;
      }
      if (localStorage.getItem(KEY) === ver) {
        return false;
      }
      sessionStorage.setItem(flagKey(ver), "1");
    } catch (e) {
      return false;
    }
    reloadBusted("_cv", ver);
    return true;
  }

  // أولاً: تحديث إجباري بعد الدخول
  try {
    if (hardRefreshAfterLogin()) return;
  } catch (e) {}

  stripParams();

  try {
    if (applyVersion(htmlVer)) return;
  } catch (e) {}

  try {
    fetch("/client-version/", { cache: "no-store", credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.text() : ""; })
      .then(function (ver) { applyVersion(ver); })
      .catch(function () {});
  } catch (e) {}

  window.addEventListener("pageshow", function (ev) {
    if (!ev.persisted) return;
    try {
      var stored = localStorage.getItem(KEY) || "";
      if (htmlVer && stored && stored !== htmlVer) {
        applyVersion(htmlVer);
      }
    } catch (e) {}
  });
})();
