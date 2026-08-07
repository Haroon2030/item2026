(function () {
  var sidebar = document.getElementById("app-sidebar");
  var openBtn = document.getElementById("sidebar-open");
  var closeBtn = document.getElementById("sidebar-close");
  var collapseBtn = document.getElementById("sidebar-collapse");
  var scrim = document.getElementById("sidebar-scrim");
  if (!sidebar) return;

  var STORAGE_KEY = "sidebar-collapsed";
  var desktopMq = window.matchMedia("(min-width: 901px)");

  function isDesktop() {
    return desktopMq.matches;
  }

  function setMobileOpen(open) {
    document.body.classList.toggle("sidebar-open", open);
    if (openBtn) openBtn.setAttribute("aria-expanded", open ? "true" : "false");
    if (scrim) {
      if (open) scrim.removeAttribute("hidden");
      else scrim.setAttribute("hidden", "");
    }
  }

  function setCollapsed(collapsed) {
    document.documentElement.classList.toggle("sidebar-collapsed", collapsed);
    if (collapseBtn) {
      collapseBtn.setAttribute("aria-expanded", collapsed ? "false" : "true");
      collapseBtn.setAttribute(
        "aria-label",
        collapsed ? "توسيع الشريط الجانبي" : "طي الشريط الجانبي"
      );
      collapseBtn.title = collapsed ? "توسيع الشريط" : "طي الشريط";
    }
    try {
      localStorage.setItem(STORAGE_KEY, collapsed ? "1" : "0");
    } catch (e) {}
  }

  function readCollapsed() {
    try {
      return localStorage.getItem(STORAGE_KEY) === "1";
    } catch (e) {
      return false;
    }
  }

  // Sync early <html> class with button state
  setCollapsed(readCollapsed());

  if (collapseBtn) {
    collapseBtn.addEventListener("click", function () {
      if (!isDesktop()) return;
      setCollapsed(!document.documentElement.classList.contains("sidebar-collapsed"));
    });
  }

  if (openBtn) {
    openBtn.addEventListener("click", function () {
      setMobileOpen(true);
    });
  }
  if (closeBtn) {
    closeBtn.addEventListener("click", function () {
      setMobileOpen(false);
    });
  }
  if (scrim) {
    scrim.addEventListener("click", function () {
      setMobileOpen(false);
    });
  }
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") setMobileOpen(false);
  });

  function onViewportChange() {
    if (isDesktop()) {
      setMobileOpen(false);
      setCollapsed(readCollapsed());
    }
  }
  if (desktopMq.addEventListener) {
    desktopMq.addEventListener("change", onViewportChange);
  } else if (desktopMq.addListener) {
    desktopMq.addListener(onViewportChange);
  }

  // أقسام فرعية في الشريط الجانبي
  sidebar.querySelectorAll("[data-sidebar-group]").forEach(function (group) {
    var toggle = group.querySelector(".sidebar-group-toggle");
    if (!toggle) return;
    toggle.addEventListener("click", function () {
      // عند طي الشريط: افتح أول رابط فرعي بدلاً من الطي/الفتح
      if (document.documentElement.classList.contains("sidebar-collapsed")) {
        var first = group.querySelector(".sidebar-sublink");
        if (first && first.href) {
          window.location.href = first.href;
        }
        return;
      }
      var open = !group.classList.contains("is-open");
      group.classList.toggle("is-open", open);
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    });
  });
})();
