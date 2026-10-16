(function () {
  "use strict";

  // قائمة المخزن تتبع الفرع المختار: بلا فرع = كل المخازن، وفرع = مخازن ذلك الفرع فقط
  var branchSel = document.getElementById("ibr-branch");
  var whSel = document.getElementById("ibr-warehouse");
  var dataEl = document.getElementById("ibr-wh-data");
  if (!branchSel || !whSel || !dataEl) return;

  var all = [];
  try { all = JSON.parse(dataEl.textContent || "[]") || []; } catch (e) { all = []; }

  function fill(keepValue) {
    var b = branchSel.value;
    var wanted = keepValue ? (whSel.getAttribute("data-selected") || "") : "";
    while (whSel.options.length > 1) whSel.remove(1);
    whSel.options[0].textContent = "كل المخازن";
    all.forEach(function (w) {
      if (b && String(w.branch_code) !== String(b)) return;
      var o = document.createElement("option");
      o.value = w.code;
      o.textContent = w.code + "- " + w.name;
      if (wanted && String(w.code) === String(wanted)) o.selected = true;
      whSel.appendChild(o);
    });
    if (!whSel.querySelector("option[selected]") && wanted) whSel.value = "";
  }

  branchSel.addEventListener("change", function () {
    whSel.setAttribute("data-selected", "");
    fill(false);
  });
  fill(true);
})();
