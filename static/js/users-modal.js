(function () {
  "use strict";

  function initUsersModal() {
    var modal = document.getElementById("users-modal");
    var openBtn = document.getElementById("users-open-add");
    if (!modal || typeof modal.showModal !== "function") return;

    var listUrl = modal.getAttribute("data-list-url") || "/users/";
    var lockClose = modal.getAttribute("data-open-on-load") === "1";

    function openModal() {
      if (modal.open) return;
      try {
        modal.showModal();
      } catch (err) {
        modal.setAttribute("open", "");
      }
      document.documentElement.classList.add("users-modal-open");
      var first = modal.querySelector("input:not([type=hidden])");
      if (first) {
        window.setTimeout(function () {
          try { first.focus(); } catch (e) {}
        }, 30);
      }
    }

    function closeModal() {
      if (lockClose) {
        window.location.href = listUrl;
        return;
      }
      if (modal.open) {
        modal.close();
      } else {
        modal.removeAttribute("open");
      }
      document.documentElement.classList.remove("users-modal-open");
    }

    if (openBtn) {
      openBtn.addEventListener("click", function (ev) {
        ev.preventDefault();
        openModal();
      });
    }

    var closeBtn = document.getElementById("users-modal-close");
    var cancelBtn = document.getElementById("users-modal-cancel");
    if (closeBtn) closeBtn.addEventListener("click", closeModal);
    if (cancelBtn) cancelBtn.addEventListener("click", closeModal);

    modal.addEventListener("cancel", function (ev) {
      if (lockClose) {
        ev.preventDefault();
        window.location.href = listUrl;
        return;
      }
      document.documentElement.classList.remove("users-modal-open");
    });

    modal.addEventListener("close", function () {
      document.documentElement.classList.remove("users-modal-open");
    });

    modal.addEventListener("click", function (ev) {
      if (ev.target === modal) closeModal();
    });

    if (lockClose) openModal();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initUsersModal);
  } else {
    initUsersModal();
  }
})();
