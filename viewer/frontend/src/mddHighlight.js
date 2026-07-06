/** Vertical MDD band overlays synced to lightweight-charts time scale. */

export const MDD_BAND_STYLES = [
  {
    bg: "rgba(248, 81, 73, 0.16)",
    border: "rgba(248, 81, 73, 0.6)",
    label: "1st MDD",
  },
  {
    bg: "rgba(210, 153, 34, 0.14)",
    border: "rgba(210, 153, 34, 0.55)",
    label: "2nd MDD",
  },
  {
    bg: "rgba(163, 113, 247, 0.14)",
    border: "rgba(163, 113, 247, 0.55)",
    label: "3rd MDD",
  },
];

function applyBandStyle(el, style) {
  if (!el || !style) return;
  el.style.background = style.bg;
  el.style.borderLeftColor = style.border;
  el.style.borderRightColor = style.border;
}

export function attachMddBand(chart, bandEl, period, style) {
  if (!chart || !bandEl) {
    return () => {};
  }
  if (style) applyBandStyle(bandEl, style);

  const update = () => {
    if (!period?.start || !period?.end) {
      bandEl.style.display = "none";
      return;
    }
    const ts = chart.timeScale();
    const x1 = ts.timeToCoordinate(period.start);
    const x2 = ts.timeToCoordinate(period.end);
    if (x1 == null || x2 == null) {
      bandEl.style.display = "none";
      return;
    }
    const left = Math.min(x1, x2);
    const width = Math.max(2, Math.abs(x2 - x1));
    bandEl.style.display = "block";
    bandEl.style.left = `${left}px`;
    bandEl.style.width = `${width}px`;
  };

  const onRange = () => update();
  chart.timeScale().subscribeVisibleLogicalRangeChange(onRange);
  window.addEventListener("resize", onRange);
  update();

  return () => {
    chart.timeScale().unsubscribeVisibleLogicalRangeChange(onRange);
    window.removeEventListener("resize", onRange);
    bandEl.style.display = "none";
  };
}

export function attachMddBands(chart, bandEls, periods) {
  const list = periods || [];
  const cleanups = (bandEls || []).map((el, i) =>
    attachMddBand(chart, el, list[i], MDD_BAND_STYLES[i])
  );
  return () => cleanups.forEach((fn) => fn());
}
