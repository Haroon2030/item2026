(function () {
  "use strict";

  var host = document.getElementById("cash-balance-chart");
  var dataEl = document.getElementById("cash-balance-data");
  if (!host || !dataEl || !window.echarts) return;

  var raw;
  try {
    raw = JSON.parse(dataEl.textContent) || [];
  } catch (e) {
    return;
  }
  if (!raw.length) return;

  var SCREEN = {
    text: "#111111", muted: "#5b5532", warn: "#9a6700", grid: "rgba(17,17,17,0.12)",
    zero: "rgba(17,17,17,0.6)", pos: "#15803d", neg: "#be123c", hatch: "rgba(255,255,255,0.6)", band: "rgba(242,200,17,0.12)"
  };
  var PRINT = {
    text: "#111827", muted: "#4b5563", warn: "#b45309", grid: "rgba(17,24,39,0.12)",
    zero: "rgba(17,24,39,0.6)", pos: "#15803d", neg: "#e11d48", hatch: "rgba(255,255,255,0.6)", band: "rgba(17,24,39,0.05)"
  };

  var rows = raw
    .map(function (r) {
      return {
        no: r.cash_no,
        name: r.cash_name || "",
        bal: Number(r.close_bal) || 0,
        disp: r.close_display || "",
        ok: !!r.ok
      };
    })
    .filter(function (r) { return r.bal !== 0; })
    .sort(function (a, b) { return b.bal - a.bal; });

  var chart = echarts.init(host, null, { renderer: "svg" });

  function compact(v) {
    var a = Math.abs(v);
    var s = a >= 1e6 ? (a / 1e6).toFixed(1).replace(/\.0$/, "") + " م" : a >= 1e3 ? Math.round(a / 1e3) + " ألف" : String(a);
    return (v < 0 ? "-" : "") + s;
  }

  function option(c) {
    return {
      animation: false,
      textStyle: { fontFamily: '"IBM Plex Sans Arabic", Tahoma, sans-serif' },
      grid: { left: 70, right: 210, top: 6, bottom: 26, containLabel: false },
      tooltip: {
        trigger: "item",
        confine: true,
        backgroundColor: "#111111",
        borderColor: "#F2C811",
        textStyle: { color: "#FDF6D8", fontSize: 13 },
        formatter: function (p) {
          var r = rows[p.dataIndex];
          return "<b>" + r.name + "</b><br/>صندوق " + r.no + "<br/>" + r.disp + "<br/>" + (r.ok ? "مطابق" : "غير مطابق");
        }
      },
      xAxis: {
        type: "value",
        splitNumber: 4,
        max: Math.max.apply(null, rows.map(function (r) { return r.bal; }).concat([0])) * 1.5,
        min: Math.min.apply(null, rows.map(function (r) { return r.bal; }).concat([0])) * 1.5,
        axisLabel: { color: c.muted, fontSize: 11.5, formatter: compact },
        axisLine: { show: false },
        axisTick: { show: false },
        splitLine: { lineStyle: { color: c.grid } }
      },
      yAxis: {
        type: "category",
        inverse: true,
        position: "right",
        data: rows.map(function (r) { return r.name; }),
        axisLine: { show: false },
        axisTick: { show: false },
        splitLine: { show: true, lineStyle: { color: c.grid, type: "dashed" } },
        splitArea: { show: true, areaStyle: { color: [c.band, "rgba(0,0,0,0)"] } },
        axisLabel: {
          align: "right",
          margin: 14,
          formatter: function (value, idx) {
            var r = rows[idx];
            return "{n|" + value + "  ·  " + r.no + "}";
          },
          rich: {
            n: { color: c.text, fontSize: 13, fontWeight: 700, width: 190, overflow: "truncate", ellipsis: "…", align: "right" }
          }
        }
      },
      series: [
        {
          type: "bar",
          barWidth: 16,
          label: {
            show: true,
            fontSize: 12,
            fontWeight: 700,
            color: c.text,
            formatter: function (p) { return "‪" + rows[p.dataIndex].disp + "‬"; },
            position: "right"
          },
          markLine: {
            silent: true,
            symbol: "none",
            label: { show: false },
            lineStyle: { color: c.zero, width: 1.5, type: "solid" },
            data: [{ xAxis: 0 }]
          },
          data: rows.map(function (r) {
            var col = r.bal < 0 ? c.neg : c.pos;
            var it = {
              value: r.bal,
              itemStyle: {
                color: col,
                borderRadius: r.bal < 0 ? [4, 0, 0, 4] : [0, 4, 4, 0]
              },
              label: r.bal < 0 ? { position: "left", align: "left" } : { position: "right", align: "right" }
            };
            if (!r.ok) {
              it.itemStyle.decal = {
                symbol: "rect", rotation: Math.PI / 4, dashArrayX: [1, 0], dashArrayY: [3, 5], color: c.hatch
              };
            }
            return it;
          })
        }
      ]
    };
  }

  host.style.height = rows.length * 32 + 40 + "px";
  chart.resize();
  chart.setOption(option(SCREEN));
  host.setAttribute(
    "aria-label",
    "أرصدة الصناديق الأعلى: " + rows.slice(0, 3).map(function (r) { return r.name + " " + r.disp; }).join("، ")
  );

  window.addEventListener("resize", function () { chart.resize(); });
  window.addEventListener("beforeprint", function () { chart.setOption(option(PRINT)); chart.resize(); });
  window.addEventListener("afterprint", function () { chart.setOption(option(SCREEN)); chart.resize(); });
})();

