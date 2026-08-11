/**
 * تحديث إجباري لمرة واحدة عند تغيّر إصدار التطبيق (بعد النشر).
 * يعتمد على data-app-ver في <html> وlocalStorage/sessionStorage.
 */
(function () {
  "use strict";

  var ver = document.documentElement.getAttribute("data-app-ver") || "";
  if (!ver) return;

  var KEY = "app-client-ver";
  var FLAG = "app-force-reload:" + ver;

  function clearCaches() {
    if (!window.caches || !caches.keys) {
      return Promise.resolve();
    }
    return caches.keys().then(function (keys) {
      return Promise.all(
        keys.map(function (k) {
          return caches.delete(k);
        })
      );
    });
  }

  try {
    // اكتمل التحديث الإجباري لهذه النسخة
    if (sessionStorage.getItem(FLAG) === "1") {
      localStorage.setItem(KEY, ver);
      sessionStorage.removeItem(FLAG);
      return;
    }

    if (localStorage.getItem(KEY) === ver) {
      return;
    }

    sessionStorage.setItem(FLAG, "1");
    clearCaches().then(
      function () {
        location.reload();
      },
      function () {
        location.reload();
      }
    );
  } catch (e) {
    // تجاهل قيود التخزين الخاص
  }
})();
