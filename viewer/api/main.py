"""FastAPI server for bear-bull drop-buy backtest visualization."""

from __future__ import annotations

import logging
import os
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

_VIEWER = Path(__file__).resolve().parents[1]
_ROOT = _VIEWER.parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from service import (  # noqa: E402
    ensure_ohlcv_current,
    list_configs,
    list_tickers,
    load_config_params,
    load_grid_schema,
    load_ohlcv_close,
    params_from_dict,
    run_viewer_backtest,
)

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def _run_startup_ohlcv_sync() -> None:
    if os.environ.get("VIEWER_SKIP_OHLCV_SYNC", "").strip().lower() in {"1", "true", "yes"}:
        logger.info("OHLCV startup sync disabled (VIEWER_SKIP_OHLCV_SYNC)")
        return
    try:
        result = ensure_ohlcv_current()
        logger.info("OHLCV startup sync: %s", result.get("reason", result))
    except Exception:
        logger.exception("OHLCV startup sync failed")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    thread = threading.Thread(target=_run_startup_ohlcv_sync, name="ohlcv-sync", daemon=True)
    thread.start()
    yield


app = FastAPI(title="Drop-Ladder Viewer API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class BacktestBody(BaseModel):
    ticker: str
    start: str
    end: str
    capital: float = Field(1.0, gt=0)
    warmup: int = Field(300, ge=0, le=2000)
    params: dict[str, Any]


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/grid-schema")
def api_grid_schema() -> dict:
    try:
        return load_grid_schema()
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@app.get("/api/configs")
def api_configs() -> dict:
    return {"configs": list_configs()}


@app.get("/api/tickers")
def api_tickers() -> dict:
    return {"tickers": list_tickers()}


@app.get("/api/ohlcv")
def api_ohlcv(
    ticker: str = Query(..., description="ticker symbol"),
    start: str = Query(None, description="YYYY-MM-DD"),
    end: str = Query(None, description="YYYY-MM-DD"),
) -> dict:
    from bear_bull_drop_buy.data_loader import default_project_db_path
    db = default_project_db_path()
    series = load_ohlcv_close(db, ticker.strip().upper(), start, end)
    return {"ticker": ticker.upper(), "series": series}


@app.get("/api/configs/{config_id}/params")
def api_config_params(config_id: str) -> dict:
    try:
        p = load_config_params(config_id)
        return {"config_id": config_id, "params": p.to_dict()}
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@app.post("/api/backtest")
def api_backtest_post(body: BacktestBody) -> dict:
    try:
        params = params_from_dict(body.params)
        return run_viewer_backtest(
            strategy_params=params,
            ticker=body.ticker.strip().upper(),
            start_date=body.start.strip(),
            end_date=body.end.strip(),
            initial_capital=body.capital,
            warmup_bars=body.warmup,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@app.get("/api/backtest")
def api_backtest(
    config_id: str = Query(..., description="config1, config2, ... (auto_trader/config<N>.json)"),
    ticker: str = Query(...),
    start: str = Query(..., description="YYYY-MM-DD"),
    end: str = Query(..., description="YYYY-MM-DD"),
    capital: float = Query(1.0, gt=0),
    warmup: int = Query(300, ge=0, le=2000),
) -> dict:
    try:
        return run_viewer_backtest(
            config_id=config_id,
            ticker=ticker.strip().upper(),
            start_date=start.strip(),
            end_date=end.strip(),
            initial_capital=capital,
            warmup_bars=warmup,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
