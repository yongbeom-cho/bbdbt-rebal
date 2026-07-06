#!/usr/bin/env python3
"""Format drift sweep JSON as markdown tables (CAGR/MDD per cell)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _cell(cagr: float | None, mdd: float | None, rebal: int | None = None) -> str:
    if cagr is None or mdd is None:
        return "-"
    base = f"{cagr:.1f}% / {mdd:.1f}%"
    if rebal is not None:
        return f"{base} ({rebal})"
    return base


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("json_path")
    ap.add_argument("--format", choices=["markdown", "tsv"], default="markdown")
    args = ap.parse_args()

    rows = json.loads(Path(args.json_path).read_text(encoding="utf-8"))
    drift_vals = sorted({r["drift_pct"] for r in rows if r.get("drift_pct") is not None})
    thresholds = drift_vals or [10.0, 12.5, 15.0, 17.5, 20.0, 22.5, 25.0]

    by_key: dict[tuple[str, str], dict] = {}
    for r in rows:
        key = (r["config_id"], r["port_label"])
        by_key.setdefault(key, {})
        if r.get("mode") == "baseline":
            by_key[key]["baseline"] = r
        else:
            drift = r.get("drift_pct")
            if drift is not None:
                by_key[key][drift] = r

    configs = sorted({k[0] for k in by_key}, key=lambda c: int(c.replace("config", "")))
    ports = sorted({k[1] for k in by_key})

    hdr = ["config", "port", "baseline"] + [f"{t:g}%" for t in thresholds]

    lines: list[str] = []
    if args.format == "markdown":
        lines.append("| " + " | ".join(hdr) + " |")
        lines.append("| " + " | ".join(["---"] * len(hdr)) + " |")
    else:
        lines.append("\t".join(hdr))

    for cfg in configs:
        for port in ports:
            key = (cfg, port)
            if key not in by_key:
                continue
            data = by_key[key]
            bl = data.get("baseline", {})
            bl_p = bl.get("portfolio", {})
            cells = [
                cfg,
                port,
                _cell(bl_p.get("cagr_pct"), bl_p.get("mdd_pct")),
            ]
            for t in thresholds:
                r = data.get(t)
                if not r:
                    cells.append("-")
                    continue
                p = r.get("portfolio", {})
                cells.append(_cell(p.get("cagr_pct"), p.get("mdd_pct"), r.get("n_rebal_events")))
            if args.format == "markdown":
                lines.append("| " + " | ".join(cells) + " |")
            else:
                lines.append("\t".join(cells))

    print("\n".join(lines))


if __name__ == "__main__":
    main()
