#!/usr/bin/env python3
"""Format multi-portfolio drift sweep JSON into separate tables."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _cell(cagr, mdd, rebal=None) -> str:
    if cagr is None or mdd is None:
        return "-"
    s = f"{cagr:.1f}% / {mdd:.1f}%"
    return f"{s} ({rebal})" if rebal is not None else s


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("json_path")
    ap.add_argument("--period-label", default="")
    args = ap.parse_args()

    rows = json.loads(Path(args.json_path).read_text(encoding="utf-8"))
    if not rows:
        return

    period = args.period_label or rows[0].get("period", "")
    start = rows[0].get("start_date", "")
    end = rows[0].get("end_date", "")

    drift_vals = sorted({r["drift_pct"] for r in rows if r.get("drift_pct") is not None})
    port_order = []
    for r in rows:
        pl = r.get("port_label", "")
        if pl and pl not in port_order:
            port_order.append(pl)

    by_key: dict[tuple[str, str], dict] = {}
    for r in rows:
        key = (r["config_id"], r["port_label"])
        by_key.setdefault(key, {})
        if r.get("mode") == "baseline":
            by_key[key]["baseline"] = r
        elif r.get("drift_pct") is not None:
            by_key[key][r["drift_pct"]] = r

    configs = sorted({k[0] for k in by_key}, key=lambda c: int(c.replace("config", "")))

    print(f"**기간**: {start} ~ {end}  (`{period}`)")
    print(f"**셀 형식**: CAGR% / MDD% (리밸 횟수)\n")

    port_names = {
        "3t": "LEV3GOLD + LEV3NASDAQ + LEV3SOX",
        "gold_ndx": "LEV3GOLD + LEV3NASDAQ",
        "gold_sox": "LEV3GOLD + LEV3SOX",
        "ndx_sox": "LEV3NASDAQ + LEV3SOX",
    }

    for port in port_order:
        title = port_names.get(port, port)
        print(f"### {title}\n")
        hdr = ["config", "baseline"] + [f"{t:g}%" for t in drift_vals]
        print("| " + " | ".join(hdr) + " |")
        print("| " + " | ".join(["---"] * len(hdr)) + " |")
        for cfg in configs:
            key = (cfg, port)
            if key not in by_key:
                continue
            data = by_key[key]
            bl = data.get("baseline", {}).get("portfolio", {})
            cells = [cfg, _cell(bl.get("cagr_pct"), bl.get("mdd_pct"))]
            for t in drift_vals:
                r = data.get(t)
                if not r:
                    cells.append("-")
                    continue
                p = r.get("portfolio", {})
                cells.append(_cell(p.get("cagr_pct"), p.get("mdd_pct"), r.get("n_rebal_events")))
            print("| " + " | ".join(cells) + " |")
        print()


if __name__ == "__main__":
    main()
