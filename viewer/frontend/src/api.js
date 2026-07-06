const API = "/api";

export async function fetchGridSchema() {
  const r = await fetch(`${API}/grid-schema`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function fetchTickers() {
  const r = await fetch(`${API}/tickers`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function fetchConfigs() {
  const r = await fetch(`${API}/configs`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function fetchConfigParams(configId) {
  const r = await fetch(`${API}/configs/${encodeURIComponent(configId)}/params`);
  if (!r.ok) {
    let msg = r.statusText;
    try {
      const j = await r.json();
      msg = j.detail || msg;
    } catch {
      /* ignore */
    }
    throw new Error(msg);
  }
  return r.json();
}

export async function fetchOhlcv(ticker, start, end) {
  const params = new URLSearchParams({ ticker });
  if (start) params.append("start", start);
  if (end) params.append("end", end);
  const r = await fetch(`${API}/ohlcv?${params}`);
  if (!r.ok) {
    let msg = r.statusText;
    try { const j = await r.json(); msg = j.detail || msg; } catch { /* ignore */ }
    throw new Error(msg);
  }
  return r.json();
}

export async function fetchBacktest({ params, ticker, start, end, capital }) {
  const r = await fetch(`${API}/backtest`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      ticker,
      start,
      end,
      capital,
      params,
    }),
  });
  if (!r.ok) {
    let msg = r.statusText;
    try {
      const j = await r.json();
      msg = j.detail || msg;
    } catch {
      /* ignore */
    }
    throw new Error(msg);
  }
  return r.json();
}
