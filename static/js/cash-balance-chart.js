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
    text: "#1F2A37", muted: "#6B7785", warn: "#F2B65C", grid: "#E3E8EF",
    zero: "#6B7785", pos: "#5BB98C", neg: "#E57373", hatch: "rgba(255,255,255,0.6)", band: "rgba(74,127,181,0.12)"
  };
  var PRINT = {
    text: "#1F2A37", muted: "#6B7785", warn: "#b45309", grid: "#E3E8EF",
    zero: "rgba(17,24,39,0.6)", pos: "#5BB98C", neg: "#E57373", hatch: "rgba(255,255,255,0.6)", band: "rgba(17,24,39,0.05)"
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

  function shortName(r) {
    var n = String(r.name || "").replace(/^\s*(ال)?صندوق\s*/, "").trim() || String(r.no);
    return n.length > 18 ? n.slice(0, 17) + "…" : n;
  }

  function option(c) {
    var maxV = Math.max.apply(null, rows.map(function (r) { return r.bal; }).concat([0]));
    var minV = Math.min.apply(null, rows.map(function (r) { return r.bal; }).concat([0]));
    var yMin = minV < -maxV * 0.02 ? minV * 1.45 : 0;
    return {
      animation: false,
      textStyle: { fontFamily: '"IBM Plex Sans Arabic", Tahoma, sans-serif' },
      grid: { left: 14, right: 20, top: 34, bottom: 120, containLabel: false },
      tooltip: {
        trigger: "item",
        confine: true,
        backgroundColor: "#FFFFFF",
        borderColor: "#E3E8EF",
        extraCssText: "box-shadow:0 4px 14px rgba(31,42,55,.12);border-radius:8px;",
        textStyle: { color: "#1F2A37", fontSize: 13 },
        formatter: function (p) {
          var r = rows[p.dataIndex];
          return "<b>" + r.name + "</b><br/>صندوق " + r.no + "<br/>" + r.disp + "<br/>" + (r.ok ? "مطابق" : "غير مطابق");
        }
      },
      xAxis: {
        type: "category",
        inverse: true,
        data: rows.map(shortName),
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { show: false }
      },
      yAxis: {
        type: "value",
        position: "right",
        min: yMin,
        max: maxV * 1.15,
        splitNumber: 4,
        axisLabel: { show: false },
        axisLine: { show: false },
        axisTick: { show: false },
        splitLine: { lineStyle: { color: c.grid } }
      },
      graphic: rows.map(function (r, i) {
        var W = chart.getWidth() || host.clientWidth;
        var gx = 14, gw = W - 14 - 20, n = rows.length;
        var x = gx + gw * (1 - (i + 0.5) / n);
        var y = (chart.getHeight() || 460) - 120 + 10;
        return {
          type: "text",
          x: x,
          y: y,
          rotation: Math.PI / 2,
          silent: true,
          style: { text: shortName(r), fill: c.text, font: "600 12px 'IBM Plex Sans Arabic', Tahoma, sans-serif", textAlign: "left", textVerticalAlign: "middle" }
        };
      }),
      series: [
        {
          type: "bar",
          barWidth: 26,
          barGap: "-100%",
          label: {
            show: true,
            fontSize: 11.5,
            fontWeight: 700,
            color: c.text,
            formatter: function (p) { return compact(rows[p.dataIndex].bal); },
            position: "top"
          },
          markLine: { silent: true, symbol: "none", label: { show: false }, lineStyle: { color: c.zero, width: 1 }, data: [{ yAxis: 0 }] },
          data: rows.map(function (r) {
            var it = {
              value: r.bal,
              itemStyle: { color: r.bal < 0 ? c.neg : c.pos, borderRadius: r.bal < 0 ? [0, 0, 5, 5] : [5, 5, 0, 0] },
              label: { position: "top" }
            };
            if (!r.ok) {
              it.itemStyle.borderColor = c.warn;
              it.itemStyle.borderWidth = 2;
            }
            return it;
          })
        }
      ]
    };
  }

  function sizeChart() {
    var wrap = host.parentElement;
    var w = Math.max(wrap ? wrap.clientWidth : 0, rows.length * 58 + 80);
    host.style.width = w + "px";
    host.style.height = "460px";
    chart.resize();
  }

  sizeChart();
  chart.setOption(option(SCREEN));
  host.setAttribute(
    "aria-label",
    "أرصدة الصناديق الأعلى: " + rows.slice(0, 3).map(function (r) { return r.name + " " + r.disp; }).join("، ")
  );

  window.addEventListener("resize", sizeChart);
  window.addEventListener("beforeprint", function () { chart.setOption(option(PRINT)); chart.resize(); });
  window.addEventListener("afterprint", function () { chart.setOption(option(SCREEN)); chart.resize(); });
})();

