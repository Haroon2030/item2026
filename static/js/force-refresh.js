/**
 * تحديث إجباري لمرة واحدة بعد كل نشر.
 * يقارن data-app-ver و /client-version/ مع localStorage ثم يعيد التحميل دون كاش.
 */
(function () {
  "use strict";

  var KEY = "app-client-ver";
  var htmlVer = document.documentElement.getAttribute("data-app-ver") || "";

  function flagKey(ver) {
    return "app-force-reload:" + ver;
  }

  function stripCv() {
    try {
      var u = new URL(location.href);
      if (!u.searchParams.has("_cv")) return;
      u.searchParams.delete("_cv");
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

  function reloadWith(ver) {
    var u;
    try {
      u = new URL(location.href);
      u.searchParams.set("_cv", ver);
    } catch (e) {
      location.reload();
      return;
    }
    clearSiteCaches().then(
      function () { location.replace(u.href); },
      function () { location.replace(u.href); }
    );
  }

  function applyVersion(ver) {
    ver = String(ver || "").trim();
    if (!ver) return false;
    try {
      if (sessionStorage.getItem(flagKey(ver)) === "1") {
        localStorage.setItem(KEY, ver);
        sessionStorage.removeItem(flagKey(ver));
        stripCv();
        return false;
      }
      if (localStorage.getItem(KEY) === ver) {
        return false;
      }
      sessionStorage.setItem(flagKey(ver), "1");
    } catch (e) {
      return false;
    }
    reloadWith(ver);
    return true;
  }

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
