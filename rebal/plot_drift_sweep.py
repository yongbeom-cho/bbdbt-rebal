#!/usr/bin/env python3
"""Plot CAGR/MDD vs drift from sweep JSON (outputs SVG, no deps)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _scale(vals: list[float], lo: float, hi: float, y0: float, y1: float) -> list[float]:
    mn, mx = min(vals), max(vals)
    pad = (mx - mn) * 0.08 or 1.0
    mn -= pad
    mx += pad
    return [y1 - (v - mn) / (mx - mn) * (y1 - y0) for v in vals]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("json_path")
    ap.add_argument("-o", "--output", default="")
    args = ap.parse_args()

    rows = json.loads(Path(args.json_path).read_text(encoding="utf-8"))
    bl = next(r for r in rows if r["mode"] == "baseline")
    rebal = sorted([r for r in rows if r["mode"] == "rebal"], key=lambda r: r["drift_pct"])

    drift = [r["drift_pct"] for r in rebal]
    cagr = [r["portfolio"]["cagr_pct"] for r in rebal]
    mdd = [r["portfolio"]["mdd_pct"] for r in rebal]
    rebal_n = [r["n_rebal_events"] for r in rebal]

    bl_cagr = bl["portfolio"]["cagr_pct"]
    bl_mdd = bl["portfolio"]["mdd_pct"]

    W, H = 1200, 560
    ml, mr, mt, mb = 70, 70, 80, 60
    pw, ph = W - ml - mr, H - mt - mb

    x0, x1 = min(drift) - 0.3, max(drift) + 0.3

    def xpos(d: float) -> float:
        return ml + (d - x0) / (x1 - x0) * pw

    combined = cagr + mdd + [bl_cagr, bl_mdd]
    ys = _scale(combined, 0, 0, mt, mt + ph)

    def ypos(v: float) -> float:
        mn = min(combined) - (max(combined) - min(combined)) * 0.08
        mx = max(combined) + (max(combined) - min(combined)) * 0.08
        return mt + ph - (v - mn) / (mx - mn) * ph

    cagr_pts = " ".join(f"{xpos(d):.1f},{ypos(v):.1f}" for d, v in zip(drift, cagr))
    mdd_pts = " ".join(f"{xpos(d):.1f},{ypos(v):.1f}" for d, v in zip(drift, mdd))

    max_n = max(rebal_n) or 1
    bars = []
    bw = pw / len(drift) * 0.35
    for d, n in zip(drift, rebal_n):
        bx = xpos(d) - bw / 2
        bh = (n / max_n) * ph * 0.22
        by = mt + ph - bh
        bars.append(f'<rect x="{bx:.1f}" y="{by:.1f}" width="{bw:.1f}" height="{bh:.1f}" fill="#9ca3af" opacity="0.35"/>')

    # y-axis ticks
    mn = min(combined) - (max(combined) - min(combined)) * 0.08
    mx = max(combined) + (max(combined) - min(combined)) * 0.08
    yticks = []
    for t in range(int(mn // 5 * 5), int(mx // 5 * 5 + 6), 5):
        if mn <= t <= mx:
            yy = ypos(float(t))
            yticks.append(
                f'<line x1="{ml}" y1="{yy:.1f}" x2="{ml+pw}" y2="{yy:.1f}" stroke="#e5e7eb" stroke-width="1"/>'
                f'<text x="{ml-8}" y="{yy+4:.1f}" text-anchor="end" font-size="11" fill="#6b7280">{t}%</text>'
            )

    # x-axis ticks every 1%
    xticks = []
    for t in range(int(x0) + 1, int(x1)):
        xx = xpos(float(t))
        xticks.append(
            f'<line x1="{xx:.1f}" y1="{mt}" x2="{xx:.1f}" y2="{mt+ph}" stroke="#f3f4f6" stroke-width="1"/>'
            f'<text x="{xx:.1f}" y="{mt+ph+22}" text-anchor="middle" font-size="11" fill="#6b7280">{t}%</text>'
        )

    bl_cagr_y = ypos(bl_cagr)
    bl_mdd_y = ypos(bl_mdd)

    best_mdd_i = min(range(len(mdd)), key=lambda i: mdd[i])
    best_cagr_i = max(range(len(cagr)), key=lambda i: cagr[i])

    out = Path(args.output) if args.output else Path(args.json_path).with_suffix(".svg")
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">
<style>text {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }}</style>
<rect width="{W}" height="{H}" fill="#fafafa"/>
<text x="{W/2}" y="32" text-anchor="middle" font-size="16" font-weight="600" fill="#111827">config60 · LEV3GOLD + LEV3NASDAQ + LEV3SOX</text>
<text x="{W/2}" y="54" text-anchor="middle" font-size="12" fill="#6b7280">{bl['start_date']} ~ {bl['end_date']}  |  baseline {bl_cagr:.1f}% / {bl_mdd:.1f}%</text>
{''.join(yticks)}
{''.join(xticks)}
<line x1="{ml}" y1="{mt+ph}" x2="{ml+pw}" y2="{mt+ph}" stroke="#374151" stroke-width="1.5"/>
<line x1="{ml}" y1="{mt}" x2="{ml}" y2="{mt+ph}" stroke="#374151" stroke-width="1.5"/>
{''.join(bars)}
<line x1="{ml}" y1="{bl_cagr_y:.1f}" x2="{ml+pw}" y2="{bl_cagr_y:.1f}" stroke="#2563eb" stroke-width="1" stroke-dasharray="6,4" opacity="0.55"/>
<line x1="{ml}" y1="{bl_mdd_y:.1f}" x2="{ml+pw}" y2="{bl_mdd_y:.1f}" stroke="#dc2626" stroke-width="1" stroke-dasharray="6,4" opacity="0.55"/>
<polyline fill="none" stroke="#2563eb" stroke-width="2.5" points="{cagr_pts}"/>
<polyline fill="none" stroke="#dc2626" stroke-width="2.5" points="{mdd_pts}"/>
<circle cx="{xpos(drift[best_cagr_i]):.1f}" cy="{ypos(cagr[best_cagr_i]):.1f}" r="5" fill="#2563eb" stroke="white" stroke-width="1.5"/>
<circle cx="{xpos(drift[best_mdd_i]):.1f}" cy="{ypos(mdd[best_mdd_i]):.1f}" r="5" fill="#dc2626" stroke="white" stroke-width="1.5"/>
<text x="{ml+12}" y="{mt+18}" font-size="12" fill="#2563eb">● CAGR</text>
<text x="{ml+12}" y="{mt+36}" font-size="12" fill="#dc2626">● MDD</text>
<text x="{ml+12}" y="{mt+54}" font-size="11" fill="#6b7280">▭ rebal count (right scale)</text>
<text x="{ml}" y="{H-18}" font-size="12" fill="#374151">Drift threshold (%)</text>
<text x="18" y="{mt+ph/2}" font-size="12" fill="#374151" transform="rotate(-90 18 {mt+ph/2})" text-anchor="middle">CAGR / MDD (%)</text>
<text x="{mr+pw+ml-5}" y="{mt+14}" font-size="11" fill="#6b7280" text-anchor="end">rebal#</text>
<text x="{mr+pw+ml-5}" y="{mt+28}" font-size="11" fill="#6b7280" text-anchor="end">{max_n}</text>
<text x="{mr+pw+ml-5}" y="{mt+ph}" font-size="11" fill="#6b7280" text-anchor="end">0</text>
</svg>"""
    out.write_text(svg, encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
