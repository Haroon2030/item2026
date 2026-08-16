(function () {
  "use strict";

  var input = document.getElementById("vt-q");
  var table = document.querySelector(".vt-table-compact");
  if (!input || !table || !table.tBodies.length) return;

  var rows = Array.prototype.slice.call(
    table.tBodies[0].querySelectorAll("tr[data-vt-code]")
  );
  var pill = document.getElementById("vt-count");
  var empty = document.getElementById("vt-empty-search");
  var foot = document.getElementById("vt-foot");
  var total = rows.length;

  function fold(value) {
    return String(value || "")
      .toLowerCase()
      .replace(/[أإآ]/g, "ا")
      .replace(/ة/g, "ه")
      .replace(/ى/g, "ي")
      .replace(/\s+/g, " ")
      .trim();
  }

  function apply() {
    var query = fold(input.value);
    var shown = 0;
    rows.forEach(function (row) {
      var hay =
        fold(row.getAttribute("data-vt-name")) +
        " " +
        fold(row.getAttribute("data-vt-code"));
      var ok = !query || hay.indexOf(query) !== -1;
      row.hidden = !ok;
      if (!ok) return;
      shown += 1;
      var idx = row.querySelector("td.vt-num");
      if (idx) idx.textContent = String(shown);
    });
    if (pill) {
      pill.textContent = query ? shown + " / " + total : String(total);
    }
    if (empty) empty.hidden = shown > 0 || total === 0;
    if (foot) foot.hidden = shown !== total;
  }

  input.addEventListener("input", apply);
  apply();
})();
