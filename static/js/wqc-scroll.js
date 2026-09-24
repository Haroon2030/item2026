(function () {
  var groupSel = document.getElementById("wqc-group");
  function clearStaleGroup() {
    if (groupSel) groupSel.value = "";
  }

  var Cascade = window.BranchWhCascade;
  if (Cascade && typeof Cascade.bind === "function") {
    Cascade.bind({
      branch: "#wqc-br-a",
      warehouse: "#wqc-wh-a",
      data: "#wqc-wh-data",
      requireBranch: true,
      blankNeedBranch: "اختر الفرع أولاً",
      blankAll: "اختر مستودعاً",
      selected: document.getElementById("wqc-wh-a")
        ? document.getElementById("wqc-wh-a").getAttribute("data-selected") || ""
        : "",
    });
    Cascade.bind({
      branch: "#wqc-br-b",
      warehouse: "#wqc-wh-b",
      data: "#wqc-wh-data",
      requireBranch: true,
      blankNeedBranch: "اختر الفرع أولاً",
      blankAll: "اختر مستودعاً",
      selected: document.getElementById("wqc-wh-b")
        ? document.getElementById("wqc-wh-b").getAttribute("data-selected") || ""
        : "",
    });
  }

  ["wqc-br-a", "wqc-br-b", "wqc-wh-a", "wqc-wh-b"].forEach(function (id) {
    var el = document.getElementById(id);
    if (el) el.addEventListener("change", clearStaleGroup);
  });

  var wrap = document.querySelector(".wqc-scroll");
  if (!wrap) return;

  function syncScrollHint() {
    var canX = wrap.scrollWidth > wrap.clientWidth + 2;
    var canY = wrap.scrollHeight > wrap.clientHeight + 2;
    wrap.classList.toggle("has-x-scroll", canX);
    wrap.classList.toggle("has-y-scroll", canY);
  }

  wrap.addEventListener("scroll", function () {
    wrap.classList.add("is-scrolling");
  });

  syncScrollHint();
  window.addEventListener("resize", syncScrollHint);
  window.addEventListener("load", syncScrollHint);
})();
