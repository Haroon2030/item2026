(function () {
  // قوائم الشرائط المرسومة من الخادم: تعرض أول 12 صفاً والباقي بالتمرير (عجلة الماوس أو سحب المؤشر)
  var VISIBLE = 12;

  function fit(list) {
    var rows = list.children;
    list.style.maxHeight = "";
    if (rows.length <= VISIBLE) return;
    var top = list.getBoundingClientRect().top;
    var last = rows[VISIBLE - 1].getBoundingClientRect().bottom;
    list.style.maxHeight = Math.ceil(last - top) + "px";
  }

  function drag(list) {
    var start = null;
    list.addEventListener("pointerdown", function (e) {
      if (e.button !== 0 || e.pointerType === "touch") return;
      start = { y: e.clientY, top: list.scrollTop, moved: false };
    });
    list.addEventListener("pointermove", function (e) {
      if (!start) return;
      var dy = e.clientY - start.y;
      if (!start.moved && Math.abs(dy) < 4) return;
      if (!start.moved) {
        start.moved = true;
        list.classList.add("is-dragging");
        try { list.setPointerCapture(e.pointerId); } catch (err) { /* لا التقاط */ }
      }
      list.scrollTop = start.top - dy;
      e.preventDefault();
    });
    function end() { start = null; list.classList.remove("is-dragging"); }
    list.addEventListener("pointerup", end);
    list.addEventListener("pointercancel", end);
  }

  document.querySelectorAll(".rb-board[data-rb-static] .rb-list.is-scroll").forEach(function (list) {
    fit(list);
    drag(list);
    window.addEventListener("resize", function () { fit(list); });
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(function () { fit(list); });
  });

  // جداول تعرض عدداً محدداً من الصفوف (data-visible-rows) والباقي بالتمرير
  function fitTable(wrap) {
    var n = parseInt(wrap.getAttribute("data-visible-rows"), 10);
    var rows = wrap.querySelectorAll("tbody tr");
    wrap.style.maxHeight = "";
    if (!n || rows.length <= n) return;
    var top = wrap.getBoundingClientRect().top;
    var last = rows[n - 1].getBoundingClientRect().bottom;
    wrap.style.setProperty("max-height", Math.ceil(last - top + 2) + "px", "important");
  }
  document.querySelectorAll(".table-wrap[data-visible-rows]").forEach(function (wrap) {
    fitTable(wrap);
    window.addEventListener("resize", function () { fitTable(wrap); });
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(function () { fitTable(wrap); });
  });

  // المبلغ يتقلص حتى يتّسع لخليته ويبقى في الوسط
  function fitAmounts(board) {
    board.querySelectorAll(".rb-meta").forEach(function (el) {
      el.style.removeProperty("font-size");
      var size = parseFloat(getComputedStyle(el).fontSize);
      while (size > 9 && el.scrollWidth > el.clientWidth + 1) {
        size -= 0.5;
        el.style.setProperty("font-size", size + "px", "important");
      }
    });
  }
  document.querySelectorAll(".rb-board[data-rb-static]").forEach(function (board) {
    fitAmounts(board);
    window.addEventListener("resize", function () { fitAmounts(board); });
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(function () { fitAmounts(board); });
  });
})();
