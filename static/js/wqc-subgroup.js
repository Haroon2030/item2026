(function () {
  "use strict";

  // المجموعة الفرعية تتبع المجموعة المختارة: تُملأ من JSON الصفحة وتُعطَّل بلا مجموعة
  var groupSel = document.getElementById("wqc-group");
  var subSel = document.getElementById("wqc-subgroup");
  var dataEl = document.getElementById("wqc-subgroup-data");
  if (!groupSel || !subSel || !dataEl) return;

  var all = [];
  try { all = JSON.parse(dataEl.textContent || "[]") || []; } catch (e) { all = []; }

  function fill(keepValue) {
    var g = groupSel.value;
    var wanted = keepValue ? (subSel.getAttribute("data-selected") || "") : "";
    while (subSel.options.length > 1) subSel.remove(1);
    subSel.options[0].textContent = g ? "كل المجموعات الفرعية" : "اختر المجموعة أولاً";
    subSel.disabled = !g;
    if (!g) { subSel.value = ""; return; }
    all.forEach(function (s) {
      if (String(s.group_code) !== String(g)) return;
      var o = document.createElement("option");
      o.value = s.code;
      o.textContent = s.code + "- " + s.name;
      if (wanted && String(s.code) === String(wanted)) o.selected = true;
      subSel.appendChild(o);
    });
  }

  groupSel.addEventListener("change", function () { subSel.setAttribute("data-selected", ""); fill(false); });
  fill(true);
})();
