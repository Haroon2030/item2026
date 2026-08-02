(function () {
  "use strict";

  function selectedLabel(select) {
    var opt = select.options[select.selectedIndex];
    return opt ? String(opt.textContent || "").trim() : "";
  }

  function closeAll(except) {
    document.querySelectorAll(".inv-select.is-open").forEach(function (wrap) {
      if (except && wrap === except) return;
      wrap.classList.remove("is-open");
      var menu = wrap.querySelector(".inv-select-menu");
      var btn = wrap.querySelector(".inv-select-trigger");
      if (menu) menu.hidden = true;
      if (btn) btn.setAttribute("aria-expanded", "false");
    });
  }

  function buildMenu(select, menu) {
    menu.innerHTML = "";
    Array.prototype.forEach.call(select.options, function (opt, idx) {
      var item = document.createElement("button");
      item.type = "button";
      item.className = "inv-select-option";
      item.setAttribute("role", "option");
      item.dataset.value = opt.value;
      item.dataset.index = String(idx);
      item.textContent = String(opt.textContent || "").trim();
      if (opt.disabled) item.disabled = true;
      if (opt.selected) {
        item.classList.add("is-selected");
        item.setAttribute("aria-selected", "true");
      } else {
        item.setAttribute("aria-selected", "false");
      }
      menu.appendChild(item);
    });
  }

  function enhance(select) {
    if (!select || select.dataset.invSelectReady === "1") return;
    select.dataset.invSelectReady = "1";

    var wrap = document.createElement("div");
    wrap.className = "inv-select";
    select.parentNode.insertBefore(wrap, select);
    wrap.appendChild(select);
    select.classList.add("inv-select-native");
    select.tabIndex = -1;

    var trigger = document.createElement("button");
    trigger.type = "button";
    trigger.className = "inv-select-trigger";
    trigger.setAttribute("aria-haspopup", "listbox");
    trigger.setAttribute("aria-expanded", "false");
    trigger.innerHTML =
      '<span class="inv-select-value"></span>' +
      '<span class="inv-select-caret" aria-hidden="true">' +
      '<svg viewBox="0 0 24 24"><path d="M6 9l6 6 6-6"/></svg>' +
      "</span>";

    var menu = document.createElement("div");
    menu.className = "inv-select-menu";
    menu.setAttribute("role", "listbox");
    menu.hidden = true;

    wrap.appendChild(trigger);
    wrap.appendChild(menu);

    var valueEl = trigger.querySelector(".inv-select-value");

    function sync() {
      valueEl.textContent = selectedLabel(select) || "—";
      buildMenu(select, menu);
    }

    function open() {
      closeAll(wrap);
      buildMenu(select, menu);
      wrap.classList.add("is-open");
      menu.hidden = false;
      trigger.setAttribute("aria-expanded", "true");
      var selected = menu.querySelector(".inv-select-option.is-selected");
      if (selected) {
        selected.scrollIntoView({ block: "nearest" });
      }
    }

    function close() {
      wrap.classList.remove("is-open");
      menu.hidden = true;
      trigger.setAttribute("aria-expanded", "false");
    }

    trigger.addEventListener("click", function (e) {
      e.preventDefault();
      if (wrap.classList.contains("is-open")) close();
      else open();
    });

    menu.addEventListener("click", function (e) {
      var item = e.target.closest(".inv-select-option");
      if (!item || item.disabled) return;
      select.value = item.dataset.value;
      select.dispatchEvent(new Event("change", { bubbles: true }));
      sync();
      close();
      trigger.focus();
    });

    select.addEventListener("change", sync);
    sync();
  }

  function init() {
    document.querySelectorAll(".inv-filter select").forEach(enhance);
  }

  document.addEventListener("click", function (e) {
    if (!e.target.closest(".inv-select")) closeAll();
  });

  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") closeAll();
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
