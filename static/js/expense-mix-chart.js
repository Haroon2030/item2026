(function () {
  "use strict";

  var host = document.getElementById("expense-mix-chart");
  var dataEl = document.getElementById("expense-mix-data");
  if (!host || !dataEl || !window.echarts) return;

  var slices;
  try {
    slices = JSON.parse(dataEl.textContent) || [];
  } catch (e) {
    return;
  }
  if (!slices.length) return;

  var SCREEN = { text: "#111111", sub: "#3a3a3a", muted: "#5b5532", grid: "rgba(17,17,17,0.12)", rest: "#BFB27A", bar: "#be123c" };
  var PRINT = { text: "#111827", sub: "#374151", muted: "#4b5563", grid: "rgba(17,24,39,0.12)", rest: "#9ca3af", bar: "#e11d48" };
  var chart = echarts.init(host, null, { renderer: "svg" });

  var rows = slices.map(function (s) {
    return {
      name: s.full_name || s.name || "",
      amount: Number(s.amount) || 0,
      share: Number(s.share_pct) || 0,
      compact: s.amount_compact || "",
      full: s.amount_display || "",
      rest: String(s.full_name || "").indexOf("باقي بنود") === 0
    };
  });
  var peak = Math.max.apply(null, rows.map(function (r) { return r.amount; })) || 1;

  function size() {
    var h = rows.length * 46 + 28;
    host.style.height = h + "px";
    chart.resize();
  }

  function option(c) {
    return {
      animation: false,
      textStyle: { fontFamily: '"IBM Plex Sans Arabic", Tahoma, sans-serif' },
      grid: { left: 8, right: 196, top: 4, bottom: 4, containLabel: false },
      tooltip: {
        trigger: "item",
        confine: true,
        backgroundColor: "#111111",
        borderColor: "#F2C811",
        textStyle: { color: "#FDF6D8", fontSize: 13 },
        formatter: function (p) {
          var r = rows[p.dataIndex];
          return "<b>" + r.name + "</b><br/>" + r.full + "<br/>" + r.share.toFixed(1) + "% من المصروف";
        }
      },
      xAxis: { type: "value", inverse: true, max: peak * 1.22, show: false },
      yAxis: {
        type: "category",
        inverse: true,
        position: "right",
        data: rows.map(function (r) { return r.name; }),
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: {
          align: "right",
          margin: 12,
          lineHeight: 18,
          formatter: function (value, idx) {
            var r = rows[idx];
            return "{n|" + value + "}\n{s|" + r.compact + "  ·  " + r.share.toFixed(1) + "%}";
          },
          rich: {
            n: { color: c.text, fontSize: 13, fontWeight: 700, width: 172, overflow: "truncate", ellipsis: "…", align: "right" },
            a: { color: c.text, fontSize: 12.5, fontWeight: 700 },
            s: { color: c.sub, fontSize: 12.5, fontWeight: 600 }
          }
        }
      },
      series: [
        {
          type: "bar",
          barWidth: 18,
          showBackground: true,
          backgroundStyle: { color: c.grid, borderRadius: 4 },
          itemStyle: { borderRadius: 4 },
          label: { show: false },
          data: rows.map(function (r) {
            return { value: r.amount, itemStyle: { color: r.rest ? c.rest : c.bar } };
          })
        }
      ]
    };
  }

  size();
  chart.setOption(option(SCREEN));
  host.setAttribute(
    "aria-label",
    "أعلى بنود المصروفات: " +
      rows.slice(0, 3).map(function (r) { return r.name + " " + r.share.toFixed(1) + "%"; }).join("، ")
  );

  window.addEventListener("resize", function () { chart.resize(); });
  window.addEventListener("beforeprint", function () {
    chart.setOption(option(PRINT));
    chart.resize();
  });
  window.addEventListener("afterprint", function () {
    chart.setOption(option(SCREEN));
    chart.resize();
  });
})();
