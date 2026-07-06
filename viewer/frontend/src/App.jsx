import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import {
  fetchBacktest,
  fetchConfigParams,
  fetchConfigs,
  fetchGridSchema,
  fetchOhlcv,
  fetchTickers,
} from "./api.js";
import Charts from "./Charts.jsx";
import ErrorBoundary from "./ErrorBoundary.jsx";
import StrategyParamsForm from "./StrategyParamsForm.jsx";
import { applyGridPreset } from "./strategyParams.js";

function pct(v) {
  if (v == null || Number.isNaN(v)) return "—";
  return `${(v * 100).toFixed(2)}%`;
}

const INITIAL_CAPITAL = 1;

function todayIso() {
  const d = new Date();
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function defaultEndDate(runEnd, tickerMeta) {
  const today = todayIso();
  if (runEnd) return runEnd;
  if (tickerMeta?.max_date && tickerMeta.max_date > today) return tickerMeta.max_date;
  return today;
}

function equityEnd(v) {
  if (v == null || Number.isNaN(v)) return "—";
  return Number(v).toFixed(4);
}

function notional(v) {
  if (v == null || Number.isNaN(v)) return "—";
  return Number(v).toFixed(4);
}

function diffPct(a, b) {
  if (a == null || b == null || Number.isNaN(a) || Number.isNaN(b)) return "—";
  const d = (a - b) * 100;
  const sign = d >= 0 ? "+" : "";
  return `${sign}${d.toFixed(2)}%p`;
}

const MDD_RANK_LABELS = ["1st", "2nd", "3rd"];

function StatsCompare({ stats, nTrades, period, tradeBreakdown, runParams, mddPeriods }) {
  const strat = stats?.strategy;
  const bh = stats?.buy_hold;
  if (!strat || !bh) return null;
  const tb = tradeBreakdown;
  const p = runParams;

  return (
    <div className="stats stats-compare">
      <table className="compare-table">
        <thead>
          <tr>
            <th></th>
            <th>전략</th>
            <th>B&amp;H</th>
            <th>차이</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td>총 수익률</td>
            <td>{pct(strat.total_pnl)}</td>
            <td>{pct(bh.total_pnl)}</td>
            <td className="diff">{diffPct(strat.total_pnl, bh.total_pnl)}</td>
          </tr>
          <tr>
            <td>CAGR</td>
            <td>{pct(strat.cagr)}</td>
            <td>{pct(bh.cagr)}</td>
            <td className="diff">{diffPct(strat.cagr, bh.cagr)}</td>
          </tr>
          <tr>
            <td>MDD</td>
            <td>{pct(strat.mdd)}</td>
            <td>{pct(bh.mdd)}</td>
            <td className="diff">{diffPct(strat.mdd, bh.mdd)}</td>
          </tr>
          <tr>
            <td>최종 자산 (초기=1)</td>
            <td>{equityEnd(strat.final_equity)}</td>
            <td>{equityEnd(bh.final_equity)}</td>
            <td>—</td>
          </tr>
        </tbody>
      </table>
      <dl className="meta-dl">
        {p && (
          <>
            <dt>regime</dt>
            <dd>
              {p.regime_ma_type} · period={p.period} · d={p.d_interval}
            </dd>
          </>
        )}
        <dt>체결 건수</dt>
        <dd>{nTrades}</dd>
        {tb && (
          <>
            <dt>Drop 체결</dt>
            <dd>{tb.drop_fills}</dd>
            <dt>기말 lot</dt>
            <dd>{tb.lots_end}</dd>
          </>
        )}
        {(mddPeriods || []).map((mp) => (
          <Fragment key={`mdd-${mp.rank ?? mp.start}`}>
            <dt>{MDD_RANK_LABELS[(mp.rank ?? 1) - 1] || `${mp.rank}th`} MDD</dt>
            <dd>
              {mp.start} → {mp.end}
              {mp.drawdown_pct != null ? ` · ${pct(mp.drawdown_pct)}` : ""}
            </dd>
          </Fragment>
        ))}
        <dt>거래일</dt>
        <dd>{strat.n_trading_days}</dd>
        <dt>기간</dt>
        <dd>
          {period?.start} ~ {period?.end}
        </dd>
      </dl>
      <p className="hint bh-hint">
        B&amp;H: 기간 첫날 매수·마지막날 청산.
      </p>
    </div>
  );
}

export default function App() {
  const [gridSchema, setGridSchema] = useState(null);
  const [configs, setConfigs] = useState([]);
  const [selectedConfigId, setSelectedConfigId] = useState("");
  const [configLoadErr, setConfigLoadErr] = useState("");
  const [strategyParams, setStrategyParams] = useState(null);
  const [tickers, setTickers] = useState([]);
  const [ticker, setTicker] = useState("");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [loading, setLoading] = useState(false);
  const [bootErr, setBootErr] = useState("");
  const [runErr, setRunErr] = useState("");
  const [result, setResult] = useState(null);
  const [fredHistory, setFredHistory] = useState(null);

  useEffect(() => {
    (async () => {
      try {
        const [schema, t, cfg] = await Promise.all([
          fetchGridSchema(),
          fetchTickers(),
          fetchConfigs(),
        ]);
        setGridSchema(schema);
        setConfigs(cfg.configs || []);
        setStrategyParams(applyGridPreset(schema, null, "default"));
        const list = t.tickers || [];
        setTickers(list);
        const run = schema.viewer_run || {};
        const preferred = run.ticker
          ? list.find((x) => x.ticker === run.ticker)
          : null;
        if (preferred) {
          setTicker(preferred.ticker);
          setStart(run.start || preferred.min_date);
          setEnd(defaultEndDate(run.end, preferred));
        } else if (list.length) {
          setTicker(list[0].ticker);
          setStart(list[0].min_date);
          setEnd(defaultEndDate(null, list[0]));
        }
      } catch (e) {
        setBootErr(String(e.message || e));
      }
    })();
  }, []);

  const tickerMeta = useMemo(
    () => tickers.find((x) => x.ticker === ticker),
    [tickers, ticker]
  );

  const onParamChange = useCallback((key, value) => {
    setStrategyParams((prev) => ({ ...prev, [key]: value }));
  }, []);

  const onPreset = useCallback(
    (name) => {
      if (!gridSchema) return;
      setSelectedConfigId("");
      setConfigLoadErr("");
      setStrategyParams((prev) => applyGridPreset(gridSchema, prev, name));
      if (name === "default" && gridSchema.viewer_run?.ticker) {
        const run = gridSchema.viewer_run;
        const m = tickers.find((x) => x.ticker === run.ticker);
        if (m) {
          setTicker(m.ticker);
          setStart(run.start || m.min_date);
          setEnd(defaultEndDate(run.end, m));
        }
      }
    },
    [gridSchema, tickers]
  );

  const selectedConfig = useMemo(
    () => configs.find((c) => c.id === selectedConfigId),
    [configs, selectedConfigId]
  );

  const onConfigSelect = useCallback(async (configId) => {
    setSelectedConfigId(configId);
    setConfigLoadErr("");
    if (!configId) return;
    try {
      const { params } = await fetchConfigParams(configId);
      setStrategyParams(params);
      const meta = configs.find((c) => c.id === configId);
      const first = meta?.strategy_tickers?.[0];
      if (first) {
        setTicker(first);
        const m = tickers.find((x) => x.ticker === first);
        if (m) {
          setStart(m.min_date);
          setEnd(defaultEndDate(null, m));
        }
      }
    } catch (e) {
      setConfigLoadErr(String(e.message || e));
    }
  }, [configs, tickers]);

  const onTickerChange = (next) => {
    setTicker(next);
    const m = tickers.find((x) => x.ticker === next);
    if (m) {
      setStart(m.min_date);
      setEnd(defaultEndDate(null, m));
    }
  };

  // GLD-based tickers → corresponding FRED-based extended history ticker
  const FRED_HISTORY_MAP = { GLD: "GOLD", LEV3GLD: "LEV3GOLD", LEV2GLD: "LEV2GOLD" };
  // GLD inception date — FRED history shown before this
  const GLD_INCEPTION = "2004-11-17";

  const run = useCallback(async () => {
    if (!strategyParams) return;
    setRunErr("");
    setFredHistory(null);
    setLoading(true);
    try {
      const [data] = await Promise.all([
        fetchBacktest({ params: strategyParams, ticker, start, end, capital: INITIAL_CAPITAL }),
      ]);
      setResult(data);

      // Fetch pre-GLD FRED extended history when applicable
      const fredTicker = FRED_HISTORY_MAP[ticker.toUpperCase()];
      if (fredTicker) {
        try {
          const fredData = await fetchOhlcv(fredTicker, "1968-01-01", GLD_INCEPTION);
          setFredHistory(fredData.series?.length ? fredData.series : null);
        } catch {
          // non-critical — ignore FRED fetch errors
        }
      }
    } catch (e) {
      setResult(null);
      setRunErr(String(e.message || e));
    } finally {
      setLoading(false);
    }
  }, [strategyParams, ticker, start, end]);

  return (
    <div className="app">
      <aside className="sidebar sidebar-scroll">
        <h1>Bear-Bull-Drop-Buy Viewer</h1>
        <p className="sub">
          auto_trader/config14.json 기본값 + ticker + 기간 → 백테스트 차트
        </p>

        {bootErr && <div className="error">{bootErr}</div>}

        {configs.length > 0 && (
          <div className="field config-picker">
            <label>auto_trader config</label>
            <select
              value={selectedConfigId}
              onChange={(e) => onConfigSelect(e.target.value)}
            >
              <option value="">(grid 수동 / 프리셋)</option>
              {configs.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.label}
                  {c.strategy_tickers?.length
                    ? ` — ${c.strategy_tickers.join(", ")}`
                    : ""}
                </option>
              ))}
            </select>
            {selectedConfig && (
              <p className="hint">
                {selectedConfig.path?.split("/").pop()}: WMA period=
                {selectedConfig.period}, d={selectedConfig.d_interval}
                {selectedConfig.strategy_tickers?.length
                  ? ` · tickers: ${selectedConfig.strategy_tickers.join(", ")}`
                  : ""}
              </p>
            )}
            {configLoadErr && <p className="error-inline">{configLoadErr}</p>}
          </div>
        )}

        {gridSchema && strategyParams && (
          <StrategyParamsForm
            values={strategyParams}
            axes={gridSchema.axes}
            schemaSource={gridSchema.source}
            viewerDefaultSource={gridSchema.viewer_default_source}
            onChange={(key, value) => {
              setSelectedConfigId("");
              onParamChange(key, value);
            }}
            onPreset={onPreset}
          />
        )}

        <div className="field">
          <label>Ticker</label>
          <select value={ticker} onChange={(e) => onTickerChange(e.target.value)}>
            {tickers.map((t) => (
              <option key={t.ticker} value={t.ticker}>
                {t.ticker}
              </option>
            ))}
          </select>
          {tickerMeta && (
            <p className="hint">
              선택 가능: {tickerMeta.min_date} ~ {tickerMeta.max_date} (
              {tickerMeta.n_bars.toLocaleString()}일)
            </p>
          )}
        </div>

        <div className="field-row">
          <div className="field">
            <label>시작일</label>
            <input
              type="date"
              value={start}
              min={tickerMeta?.min_date}
              max={tickerMeta?.max_date}
              onChange={(e) => setStart(e.target.value)}
            />
          </div>
          <div className="field">
            <label>종료일</label>
            <input
              type="date"
              value={end}
              min={tickerMeta?.min_date}
              max={
                tickerMeta?.max_date && tickerMeta.max_date > todayIso()
                  ? tickerMeta.max_date
                  : todayIso()
              }
              onChange={(e) => setEnd(e.target.value)}
            />
          </div>
        </div>

        {strategyParams && (
          <p className="hint">
            차트 오버레이: {strategyParams.regime_ma_type?.toUpperCase()}
            {strategyParams.period}
            {Number(strategyParams.d_interval) > 1
              ? ` (d_interval=${strategyParams.d_interval})`
              : ""}
          </p>
        )}

        <button
          className="run-btn"
          type="button"
          disabled={loading || !ticker || !strategyParams}
          onClick={run}
        >
          {loading ? "실행 중…" : "백테스트 실행"}
        </button>

        {result?.stats?.strategy && (
          <StatsCompare
            stats={result.stats}
            nTrades={result.n_trades}
            period={result.period}
            tradeBreakdown={result.trade_breakdown}
            runParams={result.params}
            mddPeriods={result.mdd_periods || (result.mdd_period ? [result.mdd_period] : [])}
          />
        )}
      </aside>

      <main className="main">
        {runErr && <div className="error">{runErr}</div>}

        {!result && !runErr && (
          <p className="panel-title">왼쪽에서 파라미터·기간 설정 후 백테스트를 실행하세요.</p>
        )}

        {result && (
          <>
            <ErrorBoundary>
              <Charts
                ohlc={result.ohlc}
                overlay={result.overlay}
                equity={result.equity}
                buyHoldEquity={result.buy_hold_equity}
                chartTrades={result.chart_trades}
                mddPeriods={
                  result.mdd_periods || (result.mdd_period ? [result.mdd_period] : [])
                }
                fredHistory={fredHistory}
              />
            </ErrorBoundary>
            {result.trades?.length > 0 && (
              <>
                <h2 className="panel-title">거래 목록</h2>
                <div className="trade-table-wrap">
                  <table className="trade-table">
                    <thead>
                      <tr>
                        <th>날짜</th>
                        <th>풀</th>
                        <th>구분</th>
                        <th>종류</th>
                        <th>lots</th>
                        <th>가격</th>
                        <th>수량</th>
                        <th>금액</th>
                      </tr>
                    </thead>
                    <tbody>
                      {result.trades.map((t, i) => (
                        <tr key={`${t.date}-${t.kind}-${i}`} className={t.side}>
                          <td>{t.date}</td>
                          <td>{t.pool}</td>
                          <td className="side">{t.side === "buy" ? "매수" : "매도"}</td>
                          <td>{t.kind}</td>
                          <td>
                            {t.n_lots}
                          </td>
                          <td>{t.price?.toFixed(4)}</td>
                          <td>{t.shares?.toFixed(4)}</td>
                          <td>{notional(t.notional)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </>
            )}
          </>
        )}
      </main>
    </div>
  );
}
