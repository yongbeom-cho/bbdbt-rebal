#!/usr/bin/env python3
"""Summarize a grid batch folder:

  A) Per run folder (each ticker × period): Buy & Hold PnL/MDD/annualized_pnl,
     then top-N rows by objective_score with total_pnl, annualized_pnl, mdd, objective_score, params.

  B) Cross-run “PnL rank consensus” top-K: per-scenario total_pnl, annualized_pnl, mdd, objective_score.

  B2) Cross-run objective rank consensus top-K for x in annualized_pnl*(1-mdd)^x
      (x = 0.125, 0.25, 0.5, 0.75, 1.0, 3.0, 5.0, 7.0, 9.0) → cross_objective_consensus_top.

  C) objective_top cross-eval: for each row in per_scenario objective_top (same params fingerprint),
     look up that combo in every scenario’s grid and emit by_scenario metrics (ranks, pnl, objective).
     Use this to see whether a ticker×period’s best-by-objective params generalize to other runs.

annualized_pnl: compound annualized return (1 + total_pnl)^(252/n_trading_days) - 1;
n_trading_days = eval bar count when OHLCV was loaded, else business-day count from period string.

Paths: relative args resolve vs cwd, then vs repo root (parent of sbin/), same as rank_grid_params_cross_runs.py.

Example:
  python3 sbin/report_grid_batch_summary.py var/grid_batches/20260510T003440Z \\
      --objective-top 3 --cross-top-k 5 --json-out var/batch_report.json

  # Cross-run consensus (B) only among combos matching fixed param subset, e.g. tier_cnt=2:
  python3 sbin/report_grid_batch_summary.py var/grid_batches/20260511T131213Z \\
      --objective-top 1 --cross-top-k 2 --cross-params-filter '{"tier_cnt": 2}' \\
      --json-out var/batch_report_2x_tier2.json
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from statistics import mean
from typing import Any

_SBIN = Path(__file__).resolve().parent
_REPO = _SBIN.parent
for _p in (_SBIN, _REPO / "src"):
    s = str(_p)
    if s not in sys.path:
        sys.path.insert(0, s)

import rank_grid_params_cross_runs as rg  # noqa: E402

from bear_bull_drop_buy.data_loader import DEFAULT_WARMUP_BARS, business_days_in_period, load_period  # noqa: E402
from bear_bull_drop_buy.metrics import annualized_cagr_trading_days, buy_and_hold_stats  # noqa: E402


# OBJECTIVE_CONSENSUS_XS = (1.0, 1.5, 2.0, 2.5, 3.0, 5.0, 7.0, 9.0)
OBJECTIVE_CONSENSUS_XS = (0.125, 0.25, 0.5, 0.75, 1.0, 3.0, 5.0, 7.0, 9.0)


def _objective_consensus_key(x: float) -> str:
    return f"x{x}"


def _fmt_ann(x: float) -> str:
    return f"{x:.6f}" if not math.isnan(x) else "nan"


def _objective_from_ann_mdd(annualized_pnl: float, mdd: float, x: float) -> float:
    if math.isnan(annualized_pnl):
        return float("nan")
    return annualized_pnl * ((1.0 - mdd) ** x)


def _params_match_subset(params: dict[str, Any], required: dict[str, Any]) -> bool:
    """True if params contains every key in required with equal value (==)."""
    for k, v in required.items():
        if params.get(k) != v:
            return False
    return True


def _fp_indexes_for_ok_rows(rows_ok: list[dict[str, Any]]) -> tuple[dict[str, dict], dict[str, int], dict[str, int]]:
    """Fingerprint -> row, objective rank (1=best), PnL rank (1=best) within this scenario."""
    by_fp: dict[str, dict] = {}
    for row in rows_ok:
        fp = rg.param_fingerprint(row["params"])
        by_fp[fp] = row
    sorted_obj = sorted(
        rows_ok,
        key=lambda x: float(x.get("objective_score", -1e18)),
        reverse=True,
    )
    obj_rank: dict[str, int] = {}
    for i, row in enumerate(sorted_obj, start=1):
        obj_rank[rg.param_fingerprint(row["params"])] = i
    sorted_pnl = sorted(rows_ok, key=lambda x: float(x["total_pnl"]), reverse=True)
    pnl_rank: dict[str, int] = {}
    for i, row in enumerate(sorted_pnl, start=1):
        pnl_rank[rg.param_fingerprint(row["params"])] = i
    return by_fp, obj_rank, pnl_rank


def buy_hold_for_scenario(meta: dict[str, Any], initial_capital: float) -> dict[str, Any] | None:
    ticker = str(meta.get("ticker", ""))
    period = str(meta.get("period", ""))
    if not ticker or not period or ":" not in period:
        return None
    db = meta.get("db_path") or ""
    if not db.strip():
        from bear_bull_drop_buy.data_loader import default_project_db_path

        db = default_project_db_path()
    warmup = int(meta.get("warmup_bars", DEFAULT_WARMUP_BARS))
    try:
        _, df_eval = load_period(db, ticker, period, warmup_bars=warmup)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    if df_eval is None or df_eval.empty or "close" not in df_eval.columns:
        return {"ok": False, "error": "empty_eval_or_no_close"}
    closes = df_eval["close"].to_numpy(dtype=float)
    n_td = int(len(df_eval))
    bh = buy_and_hold_stats(closes, initial_capital=initial_capital)
    ann = annualized_cagr_trading_days(bh.total_pnl, max(1, n_td))
    return {
        "ok": True,
        "total_pnl": bh.total_pnl,
        "annualized_pnl": ann,
        "mdd": bh.mdd,
        "max_drawdown_pct": bh.max_drawdown_pct,
        "final_equity": bh.final_equity,
        "n_trading_days_eval": n_td,
    }


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "roots",
        nargs="+",
        type=Path,
        help="Parent dirs of grid runs (subdirs = one scenario each)",
    )
    ap.add_argument(
        "--objective-top",
        type=int,
        default=3,
        help="Per scenario, how many best objective_score rows to show (0 = skip section A)",
    )
    ap.add_argument(
        "--cross-top-k",
        type=int,
        default=5,
        help="Cross-run PnL-rank consensus top K (0 = skip section B)",
    )
    ap.add_argument(
        "--cross-params-filter",
        default="",
        help=(
            "JSON object: section B only uses ok rows whose params match all key/value pairs "
            '(e.g. {"tier_cnt":2}). Fingerprints are still required in every scenario. '
            "Empty = no filter."
        ),
    )
    ap.add_argument(
        "--capital",
        type=float,
        default=100_000.0,
        help="Initial capital for buy&hold (match grid / run_grid_search default)",
    )
    ap.add_argument("--json-out", default="", help="Write full report as JSON")
    args = ap.parse_args()

    cross_filter: dict[str, Any] = {}
    raw_cf = (args.cross_params_filter or "").strip()
    if raw_cf:
        try:
            cross_filter = json.loads(raw_cf)
        except json.JSONDecodeError as e:
            raise SystemExit(f"--cross-params-filter: invalid JSON: {e}") from e
        if not isinstance(cross_filter, dict):
            raise SystemExit("--cross-params-filter must be a JSON object, e.g. {\"tier_cnt\": 2}")

    runs = rg.collect_runs(list(args.roots))
    if not runs:
        print("No runs found.", file=sys.stderr)
        raise SystemExit(1)

    out_doc: dict[str, Any] = {
        "initial_capital": float(args.capital),
        "annualized_note": "(1 + total_pnl)^(252 / n_trading_days) - 1; n_trading_days from eval bars or business days in period",
        "objective_consensus_xs": list(OBJECTIVE_CONSENSUS_XS),
        "objective_consensus_formula": "annualized_pnl * (1 - mdd)^x",
        "per_scenario": [],
        "cross_pnl_consensus_top": [],
        "cross_objective_consensus_top": {
            _objective_consensus_key(x): [] for x in OBJECTIVE_CONSENSUS_XS
        },
        "objective_top_cross_eval": [],
    }

    slug_nd: dict[str, int] = {}

    # --- A) per scenario: B&H + objective top N
    print("=" * 80)
    print("A) Per scenario: Buy & hold  |  top by objective_score")
    print("=" * 80)
    for name, r in runs:
        meta = r["meta"]
        ticker = meta.get("ticker")
        period = meta.get("period")
        bh = buy_hold_for_scenario(meta, args.capital)
        rows_ok = [row for row in r["rows"] if row.get("ok")]

        if bh is not None and bh.get("ok"):
            n_td_sec = int(bh["n_trading_days_eval"])
        else:
            try:
                n_td_sec = max(1, int(business_days_in_period(str(period)))) if period else 1
            except Exception:
                n_td_sec = 1
        slug_nd[name] = n_td_sec

        sec: dict[str, Any] = {
            "slug": name,
            "ticker": ticker,
            "period": period,
            "n_trading_days_eval": n_td_sec,
            "buy_hold": bh if bh is not None else {"ok": False, "error": "missing_ticker_or_period"},
            "objective_top": [],
        }
        print(f"\n### {name}")
        print(f"    ticker={ticker}  period={period}  n_trading_days={n_td_sec}")
        if bh is not None and bh.get("ok"):
            print(
                f"    Buy&Hold: pnl={bh['total_pnl']:.6f}  ann={_fmt_ann(bh['annualized_pnl'])}  "
                f"mdd={bh['mdd']:.6f}  max_dd_pct={bh['max_drawdown_pct']:.4f}"
            )
        elif bh is not None:
            print(f"    Buy&Hold: failed  {bh.get('error', bh)}")
        else:
            print("    Buy&Hold: skipped (meta)")

        if args.objective_top > 0 and rows_ok:
            by_obj = sorted(
                rows_ok,
                key=lambda x: float(x.get("objective_score", -1e18)),
                reverse=True,
            )[: args.objective_top]
            for i, row in enumerate(by_obj, start=1):
                pnl = float(row["total_pnl"])
                ann = annualized_cagr_trading_days(pnl, n_td_sec)
                mdd = float(row.get("mdd", 0.0))
                item = {
                    "rank_by_objective": i,
                    "objective_score": float(row.get("objective_score", 0.0)),
                    "total_pnl": pnl,
                    "annualized_pnl": ann,
                    "n_trading_days": n_td_sec,
                    "mdd": mdd,
                    "params": row["params"],
                }
                sec["objective_top"].append(item)
                print(
                    f"    obj#{i}  objective={item['objective_score']:.6f}  "
                    f"pnl={item['total_pnl']:.6f}  ann={_fmt_ann(ann)}  mdd={item['mdd']:.6f}"
                )
                print("    params:")
                for pline in json.dumps(row["params"], indent=2).splitlines():
                    print(f"      {pline}")
        elif args.objective_top > 0:
            print("    (no ok rows for objective_top)")

        out_doc["per_scenario"].append(sec)

    # Index every scenario grid for cross-lookup (section C)
    scenario_ledger: list[dict[str, Any]] = []
    for name, r in runs:
        ok_rows = [row for row in r["rows"] if row.get("ok")]
        by_fp, obj_rank, pnl_rank = _fp_indexes_for_ok_rows(ok_rows)
        scenario_ledger.append(
            {
                "slug": name,
                "meta": r["meta"],
                "n_trading_days": max(1, int(slug_nd.get(name, 1))),
                "n_ok": len(ok_rows),
                "by_fp": by_fp,
                "obj_rank": obj_rank,
                "pnl_rank": pnl_rank,
            }
        )

    # --- B) cross-run consensus (same as rank_grid_params_cross_runs)
    if args.cross_top_k > 0:
        print("\n" + "=" * 80)
        print("B) Cross-run PnL rank consensus top-K (params in every scenario)")
        print("=" * 80)
        if cross_filter:
            print(f"    (cross-params-filter: {json.dumps(cross_filter, sort_keys=True)})")

        scenarios: list[dict[str, Any]] = []
        for name, r in runs:
            n_td = max(1, int(slug_nd.get(name, 1)))
            ok = [
                row
                for row in r["rows"]
                if row.get("ok") and _params_match_subset(row.get("params") or {}, cross_filter)
            ]
            ok.sort(key=lambda x: float(x["total_pnl"]), reverse=True)
            ranks: dict[str, int] = {}
            pnl_by_fp: dict[str, float] = {}
            mdd_by_fp: dict[str, float] = {}
            ann_by_fp: dict[str, float] = {}
            objective_by_fp: dict[str, float] = {}
            objective_consensus_score_by_x: dict[float, dict[str, float]] = {x: {} for x in OBJECTIVE_CONSENSUS_XS}
            objective_consensus_rank_by_x: dict[float, dict[str, int]] = {x: {} for x in OBJECTIVE_CONSENSUS_XS}
            for i, row in enumerate(ok, start=1):
                fp = rg.param_fingerprint(row["params"])
                pnl = float(row["total_pnl"])
                mdd = float(row.get("mdd", 0.0))
                ann = annualized_cagr_trading_days(pnl, n_td)
                ranks[fp] = i
                pnl_by_fp[fp] = pnl
                mdd_by_fp[fp] = mdd
                ann_by_fp[fp] = ann
                objective_by_fp[fp] = float(row.get("objective_score", 0.0))
                for x in OBJECTIVE_CONSENSUS_XS:
                    objective_consensus_score_by_x[x][fp] = _objective_from_ann_mdd(ann, mdd, x)
            for x in OBJECTIVE_CONSENSUS_XS:
                sorted_fps = sorted(
                    objective_consensus_score_by_x[x],
                    key=lambda fp: (
                        float("-inf")
                        if math.isnan(objective_consensus_score_by_x[x][fp])
                        else objective_consensus_score_by_x[x][fp]
                    ),
                    reverse=True,
                )
                for i, fp in enumerate(sorted_fps, start=1):
                    objective_consensus_rank_by_x[x][fp] = i
            scenarios.append(
                {
                    "slug": name,
                    "meta": r["meta"],
                    "ranks": ranks,
                    "pnl_by_fp": pnl_by_fp,
                    "mdd_by_fp": mdd_by_fp,
                    "ann_by_fp": ann_by_fp,
                    "objective_by_fp": objective_by_fp,
                    "objective_consensus_score_by_x": objective_consensus_score_by_x,
                    "objective_consensus_rank_by_x": objective_consensus_rank_by_x,
                    "n_trading_days": n_td,
                    "n_ok": len(ok),
                }
            )

        fps_sets = [set(s["ranks"]) for s in scenarios]
        common = set.intersection(*fps_sets) if fps_sets else set()

        def sort_key(fp: str) -> tuple[float, float, float]:
            ranks_l = [s["ranks"][fp] for s in scenarios]
            pnls = [s["pnl_by_fp"][fp] for s in scenarios]
            avg_r = float(mean(ranks_l))
            worst_r = float(max(ranks_l))
            avg_pnl = float(mean(pnls))
            return (avg_r, worst_r, -avg_pnl)

        ordered = sorted(common, key=sort_key)
        top_fps = ordered[: max(0, args.cross_top_k)]

        print(f"Runs: {len(scenarios)}  Common fingerprints: {len(common)}\n")

        for rank_idx, fp in enumerate(top_fps, start=1):
            params = json.loads(fp)
            ranks_l = [s["ranks"][fp] for s in scenarios]
            pnls = [s["pnl_by_fp"][fp] for s in scenarios]
            mdds = [s["mdd_by_fp"][fp] for s in scenarios]
            anns = [
                annualized_cagr_trading_days(s["pnl_by_fp"][fp], s["n_trading_days"])
                for s in scenarios
            ]
            ann_ok = [x for x in anns if not math.isnan(x)]
            mean_ann = mean(ann_ok) if ann_ok else float("nan")
            rec = {
                "overall_rank": rank_idx,
                "mean_rank_pnl": mean(ranks_l),
                "worst_rank_pnl": max(ranks_l),
                "mean_total_pnl": mean(pnls),
                "mean_annualized_pnl": mean_ann,
                "mean_mdd": mean(mdds),
                "params": params,
                "by_scenario": [
                    {
                        "slug": s["slug"],
                        "ticker": s["meta"].get("ticker"),
                        "period": s["meta"].get("period"),
                        "n_trading_days": s["n_trading_days"],
                        "rank_by_pnl": s["ranks"][fp],
                        "total_pnl": s["pnl_by_fp"][fp],
                        "annualized_pnl": s["ann_by_fp"][fp],
                        "mdd": s["mdd_by_fp"][fp],
                        "objective_score": s["objective_by_fp"][fp],
                    }
                    for s in scenarios
                ],
            }
            out_doc["cross_pnl_consensus_top"].append(rec)
            print(
                f"--- consensus #{rank_idx}  mean_rank_pnl={rec['mean_rank_pnl']:.2f}  "
                f"worst_rank={rec['worst_rank_pnl']}  mean_pnl={rec['mean_total_pnl']:.4f}  "
                f"mean_ann={_fmt_ann(rec['mean_annualized_pnl'])}  mean_mdd={rec['mean_mdd']:.6f}"
            )
            for row in rec["by_scenario"]:
                print(
                    f"    {row['ticker']}\t{row['period']}\tn={row['n_trading_days']}\t"
                    f"pnl_rank={row['rank_by_pnl']}\tpnl={row['total_pnl']:.6f}\t"
                    f"ann={_fmt_ann(row['annualized_pnl'])}\tmdd={row['mdd']:.6f}\t"
                    f"objective={row['objective_score']:.6f}"
                )
            print("params:")
            for pline in json.dumps(params, indent=2).splitlines():
                print(f"  {pline}")

        print("\n" + "=" * 80)
        print("B2) Cross-run objective rank consensus top-K (x in annualized_pnl*(1-mdd)^x)")
        print("=" * 80)
        print(f"Runs: {len(scenarios)}  Common fingerprints: {len(common)}")
        for x in OBJECTIVE_CONSENSUS_XS:
            print(f"\n  [x={x}]")

            def sort_key_objective(fp: str) -> tuple[float, float, float]:
                ranks_l = [s["objective_consensus_rank_by_x"][x][fp] for s in scenarios]
                objs = [s["objective_consensus_score_by_x"][x][fp] for s in scenarios]
                avg_r = float(mean(ranks_l))
                worst_r = float(max(ranks_l))
                obj_ok = [v for v in objs if not math.isnan(v)]
                avg_obj = float(mean(obj_ok)) if obj_ok else float("-inf")
                return (avg_r, worst_r, -avg_obj)

            ordered_obj = sorted(common, key=sort_key_objective)
            top_fps_obj = ordered_obj[: max(0, args.cross_top_k)]

            for rank_idx, fp in enumerate(top_fps_obj, start=1):
                params = json.loads(fp)
                ranks_l = [s["objective_consensus_rank_by_x"][x][fp] for s in scenarios]
                pnls = [s["pnl_by_fp"][fp] for s in scenarios]
                mdds = [s["mdd_by_fp"][fp] for s in scenarios]
                anns = [s["ann_by_fp"][fp] for s in scenarios]
                obj_scores = [s["objective_consensus_score_by_x"][x][fp] for s in scenarios]
                ann_ok = [v for v in anns if not math.isnan(v)]
                obj_ok = [v for v in obj_scores if not math.isnan(v)]
                rec = {
                    "objective_exponent_x": x,
                    "overall_rank": rank_idx,
                    "mean_rank_objective": mean(ranks_l),
                    "worst_rank_objective": max(ranks_l),
                    "mean_objective_score": mean(obj_ok) if obj_ok else float("nan"),
                    "mean_total_pnl": mean(pnls),
                    "mean_annualized_pnl": mean(ann_ok) if ann_ok else float("nan"),
                    "mean_mdd": mean(mdds),
                    "params": params,
                    "by_scenario": [
                        {
                            "slug": s["slug"],
                            "ticker": s["meta"].get("ticker"),
                            "period": s["meta"].get("period"),
                            "n_trading_days": s["n_trading_days"],
                            "rank_by_objective": s["objective_consensus_rank_by_x"][x][fp],
                            "objective_score": s["objective_consensus_score_by_x"][x][fp],
                            "total_pnl": s["pnl_by_fp"][fp],
                            "annualized_pnl": s["ann_by_fp"][fp],
                            "mdd": s["mdd_by_fp"][fp],
                        }
                        for s in scenarios
                    ],
                }
                out_doc["cross_objective_consensus_top"][_objective_consensus_key(x)].append(rec)
                print(
                    f"--- objective consensus #{rank_idx} (x={x})  "
                    f"mean_rank_obj={rec['mean_rank_objective']:.2f}  "
                    f"worst_rank={rec['worst_rank_objective']}  "
                    f"mean_obj={_fmt_ann(rec['mean_objective_score'])}  "
                    f"mean_pnl={rec['mean_total_pnl']:.4f}  "
                    f"mean_ann={_fmt_ann(rec['mean_annualized_pnl'])}  "
                    f"mean_mdd={rec['mean_mdd']:.6f}"
                )
                for row in rec["by_scenario"]:
                    print(
                        f"    {row['ticker']}\t{row['period']}\tn={row['n_trading_days']}\t"
                        f"obj_rank={row['rank_by_objective']}\tobjective={_fmt_ann(row['objective_score'])}\t"
                        f"pnl={row['total_pnl']:.6f}\tann={_fmt_ann(row['annualized_pnl'])}\t"
                        f"mdd={row['mdd']:.6f}"
                    )
                print("params:")
                for pline in json.dumps(params, indent=2).splitlines():
                    print(f"  {pline}")

    # --- C) each objective_top row: same params evaluated on every scenario grid
    if args.objective_top > 0 and scenario_ledger:
        print("\n" + "=" * 80)
        print(
            "C) objective_top cross-eval (each source scenario’s top-by-objective params, "
            "looked up in every run)"
        )
        print("=" * 80)

        eval_idx = 0
        for sec in out_doc["per_scenario"]:
            src_slug = sec["slug"]
            for ot in sec.get("objective_top") or []:
                eval_idx += 1
                fp = rg.param_fingerprint(ot["params"])
                by_rows: list[dict[str, Any]] = []
                found_pnls: list[float] = []
                found_objs: list[float] = []
                found_mdds: list[float] = []
                found_obj_ranks: list[int] = []
                found_pnl_ranks: list[int] = []
                missing = 0
                for led in scenario_ledger:
                    if fp not in led["by_fp"]:
                        missing += 1
                        by_rows.append(
                            {
                                "slug": led["slug"],
                                "ticker": led["meta"].get("ticker"),
                                "period": led["meta"].get("period"),
                                "n_trading_days": led["n_trading_days"],
                                "n_ok_combos": led["n_ok"],
                                "in_grid": False,
                            }
                        )
                        continue
                    row = led["by_fp"][fp]
                    pnl = float(row["total_pnl"])
                    mdd = float(row.get("mdd", 0.0))
                    obj = float(row.get("objective_score", 0.0))
                    orank = int(led["obj_rank"][fp])
                    prank = int(led["pnl_rank"][fp])
                    ann = annualized_cagr_trading_days(pnl, led["n_trading_days"])
                    found_pnls.append(pnl)
                    found_objs.append(obj)
                    found_mdds.append(mdd)
                    found_obj_ranks.append(orank)
                    found_pnl_ranks.append(prank)
                    by_rows.append(
                        {
                            "slug": led["slug"],
                            "ticker": led["meta"].get("ticker"),
                            "period": led["meta"].get("period"),
                            "n_trading_days": led["n_trading_days"],
                            "n_ok_combos": led["n_ok"],
                            "in_grid": True,
                            "rank_by_objective": orank,
                            "rank_by_pnl": prank,
                            "objective_score": obj,
                            "total_pnl": pnl,
                            "annualized_pnl": ann,
                            "mdd": mdd,
                        }
                    )

                anns_ok = [
                    float(r["annualized_pnl"])
                    for r in by_rows
                    if r.get("in_grid") and not math.isnan(float(r["annualized_pnl"]))
                ]
                mean_ann = mean(anns_ok) if anns_ok else float("nan")

                rec = {
                    "eval_id": eval_idx,
                    "source_scenario": {
                        "slug": src_slug,
                        "ticker": sec.get("ticker"),
                        "period": sec.get("period"),
                        "n_trading_days": sec.get("n_trading_days_eval"),
                    },
                    "rank_by_objective_in_source": ot.get("rank_by_objective"),
                    "in_source": {
                        "objective_score": ot.get("objective_score"),
                        "total_pnl": ot.get("total_pnl"),
                        "annualized_pnl": ot.get("annualized_pnl"),
                        "mdd": ot.get("mdd"),
                    },
                    "params": ot["params"],
                    "scenarios_missing_combo": missing,
                    "mean_total_pnl": mean(found_pnls) if found_pnls else float("nan"),
                    "mean_objective_score": mean(found_objs) if found_objs else float("nan"),
                    "mean_annualized_pnl": mean_ann,
                    "mean_mdd": mean(found_mdds) if found_mdds else float("nan"),
                    "mean_rank_by_objective": mean(found_obj_ranks) if found_obj_ranks else float("nan"),
                    "worst_rank_by_objective": max(found_obj_ranks) if found_obj_ranks else None,
                    "mean_rank_by_pnl": mean(found_pnl_ranks) if found_pnl_ranks else float("nan"),
                    "worst_rank_by_pnl": max(found_pnl_ranks) if found_pnl_ranks else None,
                    "by_scenario": by_rows,
                }
                out_doc["objective_top_cross_eval"].append(rec)

                print(
                    f"--- eval #{eval_idx}  source={src_slug}  obj_rank@{src_slug}="
                    f"{ot.get('rank_by_objective')}  missing_grid={missing}/{len(scenario_ledger)}  "
                    f"mean_pnl={rec['mean_total_pnl'] if found_pnls else 'n/a'}  "
                    f"mean_obj={rec['mean_objective_score'] if found_objs else 'n/a'}"
                )
                for row in by_rows:
                    if row.get("in_grid"):
                        print(
                            f"    {row['ticker']}\t{row['period']}\tobj#={row['rank_by_objective']}\t"
                            f"pnl#={row['rank_by_pnl']}\tobjective={row['objective_score']:.6f}\t"
                            f"pnl={row['total_pnl']:.6f}\tann={_fmt_ann(row['annualized_pnl'])}\t"
                            f"mdd={row['mdd']:.6f}"
                        )
                    else:
                        print(f"    {row['ticker']}\t{row['period']}\t(no matching params in grid)")
                print("params:")
                for pline in json.dumps(ot["params"], indent=2).splitlines():
                    print(f"  {pline}")

    if args.json_out.strip():
        outp = rg.resolve_output_path(args.json_out)
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_text(json.dumps(out_doc, indent=2), encoding="utf-8")
        print(f"\nWrote {outp}")


if __name__ == "__main__":
    main()
