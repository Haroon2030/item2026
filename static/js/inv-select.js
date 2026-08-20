(function () {
  "use strict";

  var MAX_VISIBLE = 80;
  var PREVIEW_VISIBLE = 36;

  function selectedLabel(select) {
    var opt = select.options[select.selectedIndex];
    return opt ? String(opt.textContent || "").trim() : "";
  }

  function normalize(text) {
    return String(text || "")
      .toLowerCase()
      .replace(/[أإآٱ]/g, "ا")
      .replace(/ة/g, "ه")
      .replace(/ى/g, "ي")
      .replace(/ؤ/g, "و")
      .replace(/ئ/g, "ي")
      .replace(/[\u064B-\u065F\u0670]/g, "")
      .replace(/\s+/g, " ")
      .trim();
  }

  function escapeHtml(text) {
    return String(text || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function escapeRegExp(text) {
    return String(text || "").replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }

  function highlight(text, query) {
    var raw = String(text || "");
    var q = String(query || "").trim();
    if (!q) return escapeHtml(raw);
    try {
      var re = new RegExp("(" + escapeRegExp(q) + ")", "ig");
      return escapeHtml(raw).replace(re, '<mark class="inv-select-mark">$1</mark>');
    } catch (err) {
      return escapeHtml(raw);
    }
  }

  function parseLabel(label, value) {
    var text = String(label || "").trim();
    var code = String(value || "").trim();
    var sep = text.lastIndexOf(" · ");
    if (sep > 0) {
      return {
        name: text.slice(0, sep).trim(),
        code: text.slice(sep + 3).trim() || code,
        label: text,
      };
    }
    return { name: text || code || "—", code: code, label: text || code || "—" };
  }

  function closeAll(except) {
    document.querySelectorAll(".inv-select.is-open").forEach(function (wrap) {
      if (except && wrap === except) return;
      wrap.classList.remove("is-open");
      var menu = wrap.querySelector(".inv-select-menu");
      var btn = wrap.querySelector(".inv-select-trigger");
      var search = wrap.querySelector(".inv-select-search-input");
      if (menu) menu.hidden = true;
      if (btn) btn.setAttribute("aria-expanded", "false");
      if (search) search.value = "";
    });
  }

  function cacheOptions(select) {
    var out = [];
    Array.prototype.forEach.call(select.options, function (opt, idx) {
      var parsed = parseLabel(opt.textContent, opt.value);
      var unavailable = !!(opt.disabled || opt.hidden);
      out.push({
        value: String(opt.value || ""),
        name: parsed.name,
        code: parsed.code,
        label: parsed.label,
        search: normalize(parsed.name + " " + parsed.code + " " + parsed.label),
        disabled: unavailable,
        index: idx,
        blank: !String(opt.value || "").trim(),
      });
    });
    return out;
  }

  function enhance(select) {
    if (!select || select.dataset.invSelectReady === "1") return;
    select.dataset.invSelectReady = "1";

    var searchable = select.dataset.invSearch === "1";
    var keepOpen = select.dataset.invKeepOpen === "1";
    var optionsCache = cacheOptions(select);
    var activeIndex = -1;

    var wrap = document.createElement("div");
    wrap.className = "inv-select" + (searchable ? " is-searchable" : "");
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

    var searchInput = null;
    var listEl = menu;
    var metaEl = null;

    if (searchable) {
      var row = document.createElement("div");
      row.className = "inv-select-row";
      searchInput = document.createElement("input");
      searchInput.type = "search";
      searchInput.className = "inv-select-search-input";
      searchInput.setAttribute(
        "placeholder",
        select.dataset.invSearchPlaceholder || "ابحث بالاسم أو الرقم…"
      );
      searchInput.setAttribute("autocomplete", "off");
      searchInput.setAttribute("spellcheck", "false");
      searchInput.setAttribute("aria-label", "بحث في القائمة");
      metaEl = document.createElement("div");
      metaEl.className = "inv-select-meta";
      listEl = document.createElement("div");
      listEl.className = "inv-select-list";
      menu.appendChild(metaEl);
      menu.appendChild(listEl);
      row.appendChild(trigger);
      row.appendChild(searchInput);
      wrap.appendChild(row);
      wrap.appendChild(menu);
    } else {
      wrap.appendChild(trigger);
      wrap.appendChild(menu);
    }

    var valueEl = trigger.querySelector(".inv-select-value");

    function visibleItems() {
      return listEl.querySelectorAll(".inv-select-option:not([disabled])");
    }

    function setActive(next) {
      var items = visibleItems();
      if (!items.length) {
        activeIndex = -1;
        return;
      }
      if (next < 0) next = items.length - 1;
      if (next >= items.length) next = 0;
      items.forEach(function (el) {
        el.classList.remove("is-active");
      });
      activeIndex = next;
      items[activeIndex].classList.add("is-active");
      items[activeIndex].scrollIntoView({ block: "nearest" });
    }

    function renderOptions(query) {
      var q = normalize(query);
      var matched = [];
      var totalMatched = 0;
      var i;
      var row;

      for (i = 0; i < optionsCache.length; i += 1) {
        row = optionsCache[i];
        if (row.disabled) continue;
        if (!q) {
          totalMatched += 1;
          if (searchable) {
            if (row.blank || matched.length < PREVIEW_VISIBLE) matched.push(row);
          } else {
            matched.push(row);
          }
          continue;
        }
        if (searchable && q.length < 2) {
          if (row.blank) {
            totalMatched += 1;
            matched.push(row);
          }
          continue;
        }
        if (row.blank || row.search.indexOf(q) !== -1) {
          totalMatched += 1;
          if (matched.length < MAX_VISIBLE) matched.push(row);
        }
      }

      listEl.innerHTML = "";
      activeIndex = -1;

      if (!matched.length) {
        var empty = document.createElement("div");
        empty.className = "inv-select-empty";
        empty.textContent = q ? "لا نتائج مطابقة" : "لا خيارات";
        listEl.appendChild(empty);
        if (metaEl) metaEl.textContent = "0 نتيجة";
        return;
      }

      matched.forEach(function (row) {
        var item = document.createElement("button");
        item.type = "button";
        item.className = "inv-select-option" + (row.blank ? " is-blank" : "");
        item.setAttribute("role", "option");
        item.dataset.value = row.value;
        item.dataset.index = String(row.index);
        if (String(select.value) === String(row.value)) {
          item.classList.add("is-selected");
          item.setAttribute("aria-selected", "true");
        } else {
          item.setAttribute("aria-selected", "false");
        }

        if (searchable && !row.blank) {
          item.innerHTML =
            '<span class="inv-select-opt-main">' +
            '<span class="inv-select-opt-name">' +
            highlight(row.name, query) +
            "</span>" +
            (row.code
              ? '<span class="inv-select-opt-code mono">' + escapeHtml(row.code) + "</span>"
              : "") +
            "</span>";
        } else {
          item.textContent = row.label;
        }
        listEl.appendChild(item);
      });

      if (metaEl) {
        if (!q) {
          metaEl.textContent =
            "اكتب للبحث · " + optionsCache.length.toLocaleString("en-US") + " مورد";
        } else if (q.length < 2) {
          metaEl.textContent = "أكمل حرفين على الأقل لعرض المقترحات";
        } else if (totalMatched > matched.length) {
          metaEl.textContent =
            "عرض " +
            matched.length +
            " من " +
            totalMatched.toLocaleString("en-US");
        } else {
          metaEl.textContent = totalMatched.toLocaleString("en-US") + " نتيجة";
        }
      }

      var selected = listEl.querySelector(".inv-select-option.is-selected");
      if (selected) selected.scrollIntoView({ block: "nearest" });
      else if (searchable && q) setActive(0);
    }

    function syncValue() {
      valueEl.textContent = selectedLabel(select) || "—";
    }

    function sync() {
      optionsCache = cacheOptions(select);
      syncValue();
      if (!menu.hidden) renderOptions(searchInput ? searchInput.value : "");
    }

    function open() {
      // أعد قراءة الخيارات (مثل تصفية المخازن حسب الفرع)
      optionsCache = cacheOptions(select);
      closeAll(wrap);
      wrap.classList.add("is-open");
      menu.hidden = false;
      trigger.setAttribute("aria-expanded", "true");
      if (searchInput) {
        renderOptions(searchInput.value || "");
        window.setTimeout(function () {
          searchInput.focus();
        }, 0);
      } else {
        renderOptions("");
        var selected = listEl.querySelector(".inv-select-option.is-selected");
        if (selected) selected.scrollIntoView({ block: "nearest" });
      }
    }

    function close() {
      wrap.classList.remove("is-open");
      menu.hidden = true;
      trigger.setAttribute("aria-expanded", "false");
      activeIndex = -1;
      if (searchInput) searchInput.value = "";
    }

    function choose(value) {
      var want = String(value == null ? "" : value);
      var found = false;
      var i;
      for (i = 0; i < select.options.length; i += 1) {
        if (String(select.options[i].value) === want) {
          select.selectedIndex = i;
          found = true;
          break;
        }
      }
      if (!found) select.value = want;
      select.dispatchEvent(new Event("change", { bubbles: true }));
      syncValue();

      if (searchable && keepOpen) {
        if (searchInput) searchInput.value = "";
        optionsCache = cacheOptions(select);
        renderOptions("");
        var selected = listEl.querySelector(".inv-select-option.is-selected");
        if (selected) selected.scrollIntoView({ block: "nearest" });
        if (searchInput) {
          window.setTimeout(function () {
            searchInput.focus();
          }, 0);
        }
        return;
      }

      wrap.dataset.invSelectLock = "1";
      close();
      window.setTimeout(function () {
        delete wrap.dataset.invSelectLock;
        trigger.focus();
      }, 50);
    }

    trigger.addEventListener("click", function (e) {
      e.preventDefault();
      e.stopPropagation();
      if (wrap.dataset.invSelectLock === "1") return;
      if (wrap.classList.contains("is-open")) close();
      else open();
    });

    // mousedown يثبت الاختيار قبل blur لمربع البحث (click يفشل أحياناً)
    listEl.addEventListener("mousedown", function (e) {
      var item = e.target.closest(".inv-select-option");
      if (!item || item.disabled) return;
      e.preventDefault();
      e.stopPropagation();
      choose(item.dataset.value);
    });

    menu.addEventListener("mousedown", function (e) {
      // لا تغلق عند التفاعل مع رأس البحث/القائمة
      e.stopPropagation();
    });

    if (searchInput) {
      var searchTimer = null;
      searchInput.addEventListener("focus", function () {
        if (!wrap.classList.contains("is-open")) open();
      });
      searchInput.addEventListener("click", function (e) {
        e.stopPropagation();
        if (!wrap.classList.contains("is-open")) open();
      });
      searchInput.addEventListener("mousedown", function (e) {
        e.stopPropagation();
      });
      searchInput.addEventListener("input", function () {
        if (!wrap.classList.contains("is-open")) {
          wrap.classList.add("is-open");
          menu.hidden = false;
          trigger.setAttribute("aria-expanded", "true");
        }
        window.clearTimeout(searchTimer);
        searchTimer = window.setTimeout(function () {
          renderOptions(searchInput.value);
        }, 40);
      });
      searchInput.addEventListener("keydown", function (e) {
        if (e.key === "ArrowDown") {
          e.preventDefault();
          if (!wrap.classList.contains("is-open")) open();
          setActive(activeIndex < 0 ? 0 : activeIndex + 1);
        } else if (e.key === "ArrowUp") {
          e.preventDefault();
          setActive(activeIndex < 0 ? 0 : activeIndex - 1);
        } else if (e.key === "Enter") {
          e.preventDefault();
          var items = visibleItems();
          var pick = activeIndex >= 0 ? items[activeIndex] : items[0];
          if (pick) choose(pick.dataset.value);
        } else if (e.key === "Escape") {
          e.preventDefault();
          close();
          trigger.focus();
        }
      });
    }

    select.addEventListener("change", sync);
    select.addEventListener("inv-select-refresh", sync);
    syncValue();
  }

  function init() {
    document.querySelectorAll(".inv-filter select, select.warehouse-select").forEach(enhance);
  }

  window.invSelectRefresh = function (el) {
    if (!el) return;
    el.dispatchEvent(new Event("inv-select-refresh"));
  };

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
