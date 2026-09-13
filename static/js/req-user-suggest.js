/**
 * اقتراح مستخدمي الطلبات من نفس المصدر (تاريخ/فرع/مخزن).
 * يتوقع: input[data-req-user-suggest] + ul مجاور + json_script بالـ id من data-users-id
 */
(function () {
  function parseUsers(id) {
    var el = document.getElementById(id);
    if (!el) return [];
    try {
      var data = JSON.parse(el.textContent || "[]");
      return Array.isArray(data) ? data : [];
    } catch (e) {
      return [];
    }
  }

  function bind(input) {
    if (!input) return;
    var listId = input.getAttribute("aria-controls");
    var list = listId ? document.getElementById(listId) : null;
    var wrap = input.closest(".req-user-suggest");
    if (!list || !wrap) return;

    var users = parseUsers(input.getAttribute("data-users-id") || "");
    var active = -1;

    function close() {
      list.hidden = true;
      list.innerHTML = "";
      input.setAttribute("aria-expanded", "false");
      active = -1;
    }

    function pick(name, code) {
      input.value = name || code || "";
      close();
      input.focus();
    }

    function options() {
      return list.querySelectorAll(".vendor-suggest-option");
    }

    function setActive(idx) {
      var opts = options();
      active = idx;
      for (var i = 0; i < opts.length; i++) {
        opts[i].classList.toggle("is-active", i === active);
      }
      if (active >= 0 && opts[active]) {
        opts[active].scrollIntoView({ block: "nearest" });
      }
    }

    function render(q) {
      var needle = String(q || "").trim().toLowerCase();
      list.innerHTML = "";
      active = -1;
      if (!needle || !users.length) {
        close();
        return;
      }

      var hits = [];
      for (var i = 0; i < users.length; i++) {
        var u = users[i] || {};
        var code = String(u.code || "");
        var name = String(u.name || "");
        var hay = (name + " " + code).toLowerCase();
        if (hay.indexOf(needle) === -1) continue;
        hits.push({ code: code, name: name || code });
        if (hits.length >= 12) break;
      }

      if (!hits.length) {
        var empty = document.createElement("li");
        empty.className = "vendor-suggest-empty";
        empty.textContent = "لا يوجد مستخدم مطابق في طلبات المصدر";
        list.appendChild(empty);
        list.hidden = false;
        input.setAttribute("aria-expanded", "true");
        return;
      }

      hits.forEach(function (u, idx) {
        var li = document.createElement("li");
        li.setAttribute("role", "option");
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "vendor-suggest-option";
        btn.setAttribute("data-idx", String(idx));

        var nameEl = document.createElement("span");
        nameEl.className = "vendor-suggest-name";
        nameEl.textContent = u.name;
        btn.appendChild(nameEl);

        if (u.code) {
          var codeEl = document.createElement("span");
          codeEl.className = "vendor-suggest-code";
          codeEl.textContent = u.code;
          btn.appendChild(codeEl);
        }

        btn.addEventListener("mousedown", function (e) {
          e.preventDefault();
          pick(u.name, u.code);
        });
        li.appendChild(btn);
        list.appendChild(li);
      });

      list.hidden = false;
      input.setAttribute("aria-expanded", "true");
    }

    input.addEventListener("input", function () {
      render(input.value);
    });
    input.addEventListener("focus", function () {
      if (String(input.value || "").trim()) render(input.value);
    });
    input.addEventListener("keydown", function (e) {
      if (list.hidden) return;
      var opts = options();
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setActive(Math.min(active + 1, opts.length - 1));
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        setActive(Math.max(active - 1, 0));
      } else if (e.key === "Enter" && active >= 0 && opts[active]) {
        e.preventDefault();
        opts[active].click();
      } else if (e.key === "Escape") {
        close();
      }
    });

    document.addEventListener("click", function (e) {
      if (!e.target.closest(".req-user-suggest")) close();
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    document
      .querySelectorAll("input[data-req-user-suggest]")
      .forEach(bind);
  });
})();
