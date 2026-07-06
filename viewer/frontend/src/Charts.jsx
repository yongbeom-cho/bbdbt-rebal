import { useEffect, useRef } from "react";
import {
  createChart,
  CrosshairMode,
  LineStyle,
} from "lightweight-charts";
import { attachMddBands, MDD_BAND_STYLES } from "./mddHighlight.js";

const MDD_MARKER_COLORS = ["#f85149", "#d29922", "#a371f7"];
const MDD_RANK_LABELS = ["1st", "2nd", "3rd"];

const CHART_OPTS = {
  layout: {
    background: { color: "#0d1117" },
    textColor: "#8b949e",
  },
  grid: {
    vertLines: { color: "#21262d" },
    horzLines: { color: "#21262d" },
  },
  crosshair: { mode: CrosshairMode.Normal },
  rightPriceScale: { borderColor: "#30363d" },
  timeScale: {
    borderColor: "#30363d",
    timeVisible: true,
    secondsVisible: false,
  },
};

function fmtPrice(v) {
  if (v == null || Number.isNaN(v)) return "—";
  const n = Number(v);
  if (n >= 100) return n.toFixed(2);
  if (n >= 1) return n.toFixed(3);
  return n.toFixed(4);
}

function fmtPct(v) {
  if (v == null || Number.isNaN(v)) return "—";
  const sign = v >= 0 ? "+" : "";
  return `${sign}${v.toFixed(2)}%`;
}

function pctClass(v) {
  if (v == null || Number.isNaN(v) || v === 0) return "flat";
  return v > 0 ? "up" : "down";
}

function maSlopeLabel(slope) {
  if (slope === "+") return "상승 (+)";
  if (slope === "-") return "하락 (−)";
  if (slope === "0") return "보합 (0)";
  return "—";
}

function maSlopeClass(slope) {
  if (slope === "+") return "up";
  if (slope === "-") return "down";
  return "flat";
}

function overlayDisplayLabel(overlay) {
  if (!overlay?.type || overlay.type === "none") return null;
  if (overlay.label) return overlay.label;
  const t = overlay.type.toUpperCase();
  return `${t}(${overlay.period})`;
}

function buildOhlcMap(ohlc) {
  const map = new Map();
  const sorted = [...(ohlc || [])].sort((a, b) => a.time.localeCompare(b.time));
  for (let i = 0; i < sorted.length; i += 1) {
    const b = sorted[i];
    map.set(b.time, {
      ...b,
      prevClose: i > 0 ? sorted[i - 1].close : null,
    });
  }
  return map;
}

function buildValueMap(series) {
  const map = new Map();
  const sorted = [...(series || [])].sort((a, b) => a.time.localeCompare(b.time));
  for (const p of sorted) {
    map.set(p.time, p.value);
  }
  return { map, initial: sorted.length ? sorted[0].value : null };
}

function crosshairTime(param) {
  if (!param?.time) return null;
  if (typeof param.time === "string") return param.time;
  if (param.time?.year) {
    return `${param.time.year}-${String(param.time.month).padStart(2, "0")}-${String(param.time.day).padStart(2, "0")}`;
  }
  return null;
}

function fmtEquity(v) {
  if (v == null || Number.isNaN(v)) return "—";
  return Number(v).toFixed(4);
}

function fmtPctPoint(v) {
  if (v == null || Number.isNaN(v)) return "—";
  const sign = v >= 0 ? "+" : "";
  return `${sign}${v.toFixed(2)}%`;
}

function renderEquityTooltip(el, time, strat, bh, initStrat, initBh) {
  if (!el) return;
  if (!time || (strat == null && bh == null)) {
    el.className = "ohlc-tooltip equity-tooltip ohlc-tooltip--empty";
    el.innerHTML =
      '<span class="ohlc-placeholder">자산 곡선에 마우스를 올리면 전략·B&amp;H 자산이 표시됩니다</span>';
    return;
  }
  const stratPct =
    strat != null && initStrat != null && initStrat > 0
      ? ((strat / initStrat) - 1) * 100
      : null;
  const bhPct =
    bh != null && initBh != null && initBh > 0 ? ((bh / initBh) - 1) * 100 : null;
  const diffPp =
    stratPct != null && bhPct != null ? stratPct - bhPct : null;

  el.className = "ohlc-tooltip equity-tooltip";
  el.innerHTML = `
    <div class="ohlc-row ohlc-head">
      <span class="ohlc-date">${time}</span>
    </div>
    <div class="ohlc-row equity-values">
      <span class="equity-strat">
        <em>전략</em>
        <b>${fmtEquity(strat)}</b>
        <span class="ohlc-chg ${pctClass(stratPct)}">${fmtPctPoint(stratPct)}</span>
      </span>
      <span class="equity-bh">
        <em>B&amp;H</em>
        <b>${fmtEquity(bh)}</b>
        <span class="ohlc-chg ${pctClass(bhPct)}">${fmtPctPoint(bhPct)}</span>
      </span>
      ${
        diffPp != null
          ? `<span class="equity-diff ohlc-chg ${pctClass(diffPp)}">전략−B&amp;H <b>${fmtPctPoint(diffPp)}</b>p</span>`
          : ""
      }
    </div>
    <p class="equity-hint muted">초기=1 기준 누적 수익률 · 크로스헤어 점 = 해당 일자</p>
  `;
}

function renderOhlcTooltip(el, bar, overlayLabel) {
  if (!el) return;
  if (!bar) {
    el.className = "ohlc-tooltip ohlc-tooltip--empty";
    el.innerHTML =
      '<span class="ohlc-placeholder">캔들에 마우스를 올리면 시세가 표시됩니다</span>';
    return;
  }
  const closeVsOpen =
    bar.open > 0 ? ((bar.close - bar.open) / bar.open) * 100 : null;
  const closeVsPrev =
    bar.prevClose != null && bar.prevClose > 0
      ? ((bar.close - bar.prevClose) / bar.prevClose) * 100
      : null;
  const maName = overlayLabel || "MA";
  const maBlock =
    bar.overlay_value != null
      ? `<span class="ohlc-ema">${maName} <b>${fmtPrice(bar.overlay_value)}</b></span>
         <span class="ohlc-chg ${maSlopeClass(bar.overlay_slope)}">regime 기울기 ${maSlopeLabel(bar.overlay_slope)}</span>`
      : overlayLabel
        ? `<span class="ohlc-ema muted">${maName} — (warmup 구간)</span>`
        : "";

  const cashRatio = bar.cash_ratio != null ? bar.cash_ratio : null;
  const portfolioBlock =
    cashRatio != null
      ? `<span class="ohlc-portfolio-cash">현금 <b>${(cashRatio * 100).toFixed(1)}%</b></span>
         <span class="ohlc-portfolio-stock">주식 <b>${((1 - cashRatio) * 100).toFixed(1)}%</b></span>`
      : "";

  el.className = "ohlc-tooltip";
  el.innerHTML = `
    <div class="ohlc-row ohlc-head">
      <span class="ohlc-date">${bar.time}</span>
    </div>
    <div class="ohlc-row ohlc-pct-row">
      <span class="ohlc-chg ${pctClass(closeVsPrev)}">전일종가→종가 <b>${fmtPct(closeVsPrev)}</b></span>
      <span class="ohlc-chg ${pctClass(closeVsOpen)}">시가→종가 <b>${fmtPct(closeVsOpen)}</b></span>
    </div>
    <div class="ohlc-row ohlc-quotes">
      <span><em>시</em> <b>${fmtPrice(bar.open)}</b></span>
      <span><em>고</em> <b>${fmtPrice(bar.high)}</b></span>
      <span><em>저</em> <b>${fmtPrice(bar.low)}</b></span>
      <span><em>종</em> <b class="${pctClass(closeVsOpen)}">${fmtPrice(bar.close)}</b></span>
    </div>
    ${maBlock ? `<div class="ohlc-row ohlc-ema-row">${maBlock}</div>` : ""}
    ${portfolioBlock ? `<div class="ohlc-row ohlc-portfolio-row">${portfolioBlock}</div>` : ""}
  `;
}

function buildMddMarkers(periods) {
  const out = [];
  for (const period of periods || []) {
    if (!period?.start || !period?.end) continue;
    const rank = period.rank ?? out.length / 2 + 1;
    const lab = MDD_RANK_LABELS[rank - 1] || `${rank}th`;
    const color = MDD_MARKER_COLORS[rank - 1] || MDD_MARKER_COLORS[0];
    const dd =
      period.drawdown_pct != null
        ? ` ${(period.drawdown_pct * 100).toFixed(1)}%`
        : "";
    out.push(
      {
        time: period.start,
        position: "aboveBar",
        color,
        shape: "circle",
        text: `${lab} MDD 고점${dd}`,
      },
      {
        time: period.end,
        position: "belowBar",
        color,
        shape: "circle",
        text: `${lab} MDD 저점`,
      }
    );
  }
  // lightweight-charts requires markers in ascending time order
  out.sort((a, b) => String(a.time).localeCompare(String(b.time)));
  return out;
}

function buildMarkers(chartTrades) {
  const markers = (chartTrades || []).map((t) => {
    const buy = t.side === "buy";
    const action = buy ? "매수" : "매도";
    const lotN = t.n_lots ?? 0;
    const lots = `lots ${lotN}`;
    const notional = t.notional ?? 0;
    const fills = t.n_fills > 1 ? ` · ${t.n_fills}체결` : "";
    const text = `Drop ${action} | ${lots} ${notional.toFixed(4)}${fills}`;
    const color = buy ? "#3fb950" : "#f85149";
    return {
      time: t.date,
      position: buy ? "belowBar" : "aboveBar",
      color,
      shape: buy ? "arrowUp" : "arrowDown",
      text,
    };
  });
  markers.sort((a, b) => String(a.time).localeCompare(String(b.time)));
  return markers;
}

export default function Charts({
  ohlc,
  overlay,
  equity,
  buyHoldEquity,
  chartTrades,
  mddPeriods,
  fredHistory,
}) {
  const priceRef = useRef(null);
  const equityRef = useRef(null);
  const mddBandPriceRefs = useRef([null, null, null]);
  const mddBandEquityRefs = useRef([null, null, null]);
  const ohlcTooltipRef = useRef(null);
  const equityTooltipRef = useRef(null);
  const priceChartRef = useRef(null);
  const equityChartRef = useRef(null);
  const syncing = useRef(false);
  const ohlcMapRef = useRef(new Map());
  const equityMapRef = useRef(new Map());
  const bhMapRef = useRef(new Map());
  const equityInitRef = useRef(null);
  const bhInitRef = useRef(null);
  const overlayLabelRef = useRef(null);

  const overlayLabel = overlayDisplayLabel(overlay);
  overlayLabelRef.current = overlayLabel;

  useEffect(() => {
    ohlcMapRef.current = buildOhlcMap(ohlc);
    renderOhlcTooltip(ohlcTooltipRef.current, null, overlayLabelRef.current);
  }, [ohlc, overlayLabel]);

  useEffect(() => {
    const strat = buildValueMap(equity);
    const bh = buildValueMap(buyHoldEquity);
    equityMapRef.current = strat.map;
    bhMapRef.current = bh.map;
    equityInitRef.current = strat.initial;
    bhInitRef.current = bh.initial;
    renderEquityTooltip(
      equityTooltipRef.current,
      null,
      null,
      null,
      strat.initial,
      bh.initial
    );
  }, [equity, buyHoldEquity]);

  useEffect(() => {
    if (!priceRef.current || !equityRef.current) return undefined;

    const priceChart = createChart(priceRef.current, {
      ...CHART_OPTS,
      width: priceRef.current.clientWidth,
      height: 380,
    });
    const equityChart = createChart(equityRef.current, {
      ...CHART_OPTS,
      width: equityRef.current.clientWidth,
      height: 220,
    });

    priceChartRef.current = priceChart;
    equityChartRef.current = equityChart;

    const candle = priceChart.addCandlestickSeries({
      upColor: "#e5534b",
      downColor: "#388bfd",
      borderVisible: false,
      wickUpColor: "#e5534b",
      wickDownColor: "#388bfd",
    });

    let fredLine = null;
    if (fredHistory?.length) {
      fredLine = priceChart.addLineSeries({
        color: "rgba(255, 165, 0, 0.6)",
        lineWidth: 1,
        lineStyle: 2,  // dashed
        title: "FRED(pre-GLD)",
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: true,
      });
    }

    let maLine = null;
    if (overlay?.points?.length) {
      maLine = priceChart.addLineSeries({
        color: "#f0c14b",
        lineWidth: 2,
        title: overlayLabel || "MA",
        priceLineVisible: false,
        lastValueVisible: true,
        crosshairMarkerVisible: true,
      });
    }

    const equityLine = equityChart.addLineSeries({
      color: "#58a6ff",
      lineWidth: 2,
      title: "전략",
      priceLineVisible: true,
      lastValueVisible: true,
      crosshairMarkerVisible: true,
      crosshairMarkerRadius: 5,
    });

    const bhLine = equityChart.addLineSeries({
      color: "#d29922",
      lineWidth: 2,
      lineStyle: LineStyle.Dashed,
      title: "B&H",
      priceLineVisible: false,
      lastValueVisible: true,
      crosshairMarkerVisible: true,
      crosshairMarkerRadius: 5,
    });

    if (fredLine && fredHistory?.length) {
      fredLine.setData(fredHistory.map((p) => ({ time: p.time, value: p.value })));
    }

    if (ohlc?.length) {
      candle.setData(
        ohlc.map((b) => ({
          time: b.time,
          open: b.open,
          high: b.high,
          low: b.low,
          close: b.close,
        }))
      );
      if (maLine) {
        const maData = (overlay.points || []).map((p) => ({
          time: p.time,
          value: p.value,
        }));
        if (maData.length) {
          maLine.setData(maData);
        }
      }
    }

    if (chartTrades?.length) {
      candle.setMarkers(buildMarkers(chartTrades));
    }

    if (equity?.length) {
      equityLine.setData(
        equity.map((p) => ({
          time: p.time,
          value: p.value,
        }))
      );
      const mddMarkers = buildMddMarkers(mddPeriods);
      if (mddMarkers.length) {
        try {
          equityLine.setMarkers(mddMarkers);
        } catch (err) {
          console.warn("MDD markers skipped:", err);
        }
      }
    }

    if (buyHoldEquity?.length) {
      bhLine.setData(
        buyHoldEquity.map((p) => ({
          time: p.time,
          value: p.value,
        }))
      );
    }

    const syncRange = (source, target) => {
      const range = source.timeScale().getVisibleLogicalRange();
      if (!range || syncing.current) return;
      syncing.current = true;
      target.timeScale().setVisibleLogicalRange(range);
      syncing.current = false;
    };

    priceChart.timeScale().subscribeVisibleLogicalRangeChange(() => {
      syncRange(priceChart, equityChart);
    });
    equityChart.timeScale().subscribeVisibleLogicalRangeChange(() => {
      syncRange(equityChart, priceChart);
    });

    priceChart.timeScale().fitContent();
    equityChart.timeScale().fitContent();

    const detachMddPrice = attachMddBands(
      priceChart,
      mddBandPriceRefs.current,
      mddPeriods
    );
    const detachMddEquity = attachMddBands(
      equityChart,
      mddBandEquityRefs.current,
      mddPeriods
    );

    const updateEquityTooltipFromTime = (t) => {
      const tip = equityTooltipRef.current;
      if (!tip) return;
      if (!t) {
        renderEquityTooltip(
          tip,
          null,
          null,
          null,
          equityInitRef.current,
          bhInitRef.current
        );
        return;
      }
      renderEquityTooltip(
        tip,
        t,
        equityMapRef.current.get(t),
        bhMapRef.current.get(t),
        equityInitRef.current,
        bhInitRef.current
      );
    };

    const crosshairOffChart = (param) =>
      param.point === undefined || param.point.x < 0 || param.point.y < 0;

    const onPriceCrosshair = (param) => {
      const tip = ohlcTooltipRef.current;
      if (!tip || crosshairOffChart(param)) {
        renderOhlcTooltip(tip, null, overlayLabelRef.current);
        updateEquityTooltipFromTime(null);
        return;
      }
      const t = crosshairTime(param);
      if (!t) {
        renderOhlcTooltip(tip, null, overlayLabelRef.current);
        updateEquityTooltipFromTime(null);
        return;
      }
      const bar = ohlcMapRef.current.get(t);
      renderOhlcTooltip(tip, bar || null, overlayLabelRef.current);
      updateEquityTooltipFromTime(t);
    };

    const onEquityCrosshair = (param) => {
      if (crosshairOffChart(param)) {
        updateEquityTooltipFromTime(null);
        return;
      }
      const t = crosshairTime(param);
      if (!t) {
        updateEquityTooltipFromTime(null);
        return;
      }
      let strat = equityMapRef.current.get(t);
      let bh = bhMapRef.current.get(t);
      if (param.seriesData) {
        const sd = param.seriesData.get(equityLine);
        if (sd?.value != null) strat = sd.value;
        const sdBh = param.seriesData.get(bhLine);
        if (sdBh?.value != null) bh = sdBh.value;
      }
      renderEquityTooltip(
        equityTooltipRef.current,
        t,
        strat,
        bh,
        equityInitRef.current,
        bhInitRef.current
      );
    };

    priceChart.subscribeCrosshairMove(onPriceCrosshair);
    equityChart.subscribeCrosshairMove(onEquityCrosshair);

    const onResize = () => {
      if (priceRef.current) {
        priceChart.applyOptions({ width: priceRef.current.clientWidth });
      }
      if (equityRef.current) {
        equityChart.applyOptions({ width: equityRef.current.clientWidth });
      }
    };
    window.addEventListener("resize", onResize);

    return () => {
      detachMddPrice();
      detachMddEquity();
      window.removeEventListener("resize", onResize);
      priceChart.unsubscribeCrosshairMove(onPriceCrosshair);
      equityChart.unsubscribeCrosshairMove(onEquityCrosshair);
      priceChart.remove();
      equityChart.remove();
      renderOhlcTooltip(ohlcTooltipRef.current, null, overlayLabelRef.current);
      renderEquityTooltip(
        equityTooltipRef.current,
        null,
        null,
        null,
        equityInitRef.current,
        bhInitRef.current
      );
      priceChartRef.current = null;
      equityChartRef.current = null;
    };
  }, [ohlc, overlay, overlayLabel, equity, buyHoldEquity, chartTrades, mddPeriods, fredHistory]);

  return (
    <>
      <div className="legend">
        <span className="candle-up">양봉 close ≥ open (빨강)</span>
        <span className="candle-down">음봉 close &lt; open (파랑)</span>
        {overlayLabel && (
          <span className="overlay-line">{overlayLabel} (금색)</span>
        )}
        {fredHistory?.length > 0 && (
          <span className="fred-line">FRED 금 이력 pre-GLD (주황 점선)</span>
        )}
        <span className="buy">Drop 매수/매도 (녹/빨)</span>
        <span className="lots-hint">lots = 보유 lot 개수 (매매 순번 아님)</span>
        <span className="strat-line">전략 자산 (파랑)</span>
        <span className="bh-line">Buy &amp; Hold (주황 점선)</span>
        {(mddPeriods || []).map((mp) => {
          const rank = mp.rank ?? 1;
          const style = MDD_BAND_STYLES[rank - 1];
          const lab = style?.label || `${rank}th MDD`;
          if (!mp.start || !mp.end) return null;
          return (
            <span
              key={`mdd-leg-${rank}`}
              className={`mdd-band-legend mdd-band-legend-rank-${rank}`}
              title={`${mp.start} → ${mp.end}`}
            >
              {lab}: {mp.start} → {mp.end}
              {mp.drawdown_pct != null
                ? ` (−${(mp.drawdown_pct * 100).toFixed(2)}%)`
                : ""}
            </span>
          );
        })}
      </div>
      <h2 className="panel-title">OHLC + 매매</h2>
      <div className="chart-wrap chart-wrap-ohlc">
        <div className="ohlc-tooltip ohlc-tooltip--empty" ref={ohlcTooltipRef}>
          <span className="ohlc-placeholder">캔들에 마우스를 올리면 시세가 표시됩니다</span>
        </div>
        <div className="chart-stack">
          {[0, 1, 2].map((i) => (
            <div
              key={`mdd-price-${i}`}
              className={`mdd-band mdd-band-rank-${i + 1}`}
              ref={(el) => {
                mddBandPriceRefs.current[i] = el;
              }}
              aria-hidden
            />
          ))}
          <div className="chart" ref={priceRef} />
        </div>
      </div>
      <h2 className="panel-title">총자산 — 전략 vs B&amp;H (동일 X축, 스크롤/줌 연동)</h2>
      <div className="chart-wrap chart-wrap-equity">
        <div
          className="ohlc-tooltip equity-tooltip ohlc-tooltip--empty"
          ref={equityTooltipRef}
        >
          <span className="ohlc-placeholder">
            자산 곡선에 마우스를 올리면 전략·B&amp;H 자산이 표시됩니다
          </span>
        </div>
        <div className="chart-stack">
          {[0, 1, 2].map((i) => (
            <div
              key={`mdd-eq-${i}`}
              className={`mdd-band mdd-band-rank-${i + 1}`}
              ref={(el) => {
                mddBandEquityRefs.current[i] = el;
              }}
              aria-hidden
            />
          ))}
          <div className="chart equity" ref={equityRef} />
        </div>
      </div>
    </>
  );
}
