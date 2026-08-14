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

  function isCollapsed() {
    return isDesktop() && document.documentElement.classList.contains("sidebar-collapsed");
  }

  function setMobileOpen(open) {
    document.body.classList.toggle("sidebar-open", open);
    if (openBtn) {
      openBtn.setAttribute("aria-expanded", open ? "true" : "false");
      openBtn.setAttribute("aria-label", open ? "إغلاق القائمة" : "فتح القائمة");
    }
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

  function syncViewport() {
    if (isDesktop()) {
      setMobileOpen(false);
      setCollapsed(readCollapsed());
    } else {
      setMobileOpen(false);
      document.documentElement.classList.remove("sidebar-collapsed");
    }
  }

  syncViewport();

  if (collapseBtn) {
    collapseBtn.addEventListener("click", function () {
      if (!isDesktop()) return;
      setCollapsed(!document.documentElement.classList.contains("sidebar-collapsed"));
    });
  }

  if (openBtn) {
    openBtn.addEventListener("click", function () {
      if (isDesktop()) return;
      setMobileOpen(!document.body.classList.contains("sidebar-open"));
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

  sidebar.querySelectorAll("a.sidebar-link").forEach(function (link) {
    link.addEventListener("click", function () {
      if (!isDesktop()) setMobileOpen(false);
    });
  });

  if (desktopMq.addEventListener) {
    desktopMq.addEventListener("change", syncViewport);
  } else if (desktopMq.addListener) {
    desktopMq.addListener(syncViewport);
  }

  sidebar.querySelectorAll("[data-sidebar-group]").forEach(function (group) {
    var toggle = group.querySelector(".sidebar-group-toggle");
    if (!toggle) return;
    toggle.addEventListener("click", function () {
      if (isCollapsed()) {
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
