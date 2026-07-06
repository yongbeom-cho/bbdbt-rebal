# Drop-Ladder Viewer

`auto_trader/config14.json`의 `strategy_params`와 첫 번째 `strategies` ticker를 초기값으로 쓰고, 폼 축은 `configs/grid_default.json`에서 가져옵니다. 기간(start/end)은 config에 없으면 DB ticker 구간 전체를 씁니다. ticker·기간으로 백테스트한 뒤 **OHLC + 매매 마커**와 **총자산 곡선**(동일 X축 연동)을 브라우저에서 봅니다. 차트 MA 오버레이는 `regime_ma_type`·`period`·`d_interval`과 자동 일치합니다.

## 요구 사항

- Python 3.10+ (`numpy`, `pandas`, `fastapi`, `uvicorn`)
- Node.js 18+ (프론트)
- DB: `var/data/usaetf_ohlcv_day.db`
- 페이지 로드 시 DB 마지막 날짜 다음날 ~ **어제(마지막 완료 영업일)** 까지 FinanceDataReader로 자동 보충 (당일 장중 바는 제외, `LEV*`는 기초 자산 갱신 후 재생성)

## 실행

```bash
cd bear-bull-drop-buy-trading/viewer
chmod +x run_dev.sh
./run_dev.sh
```

- UI: http://127.0.0.1:5174  
- API: http://127.0.0.1:8765 (Swagger: `/docs`)

## API

| Method | Path | 설명 |
|--------|------|------|
| GET | `/api/grid-schema` | `grid_default.json` 축 + `auto_trader/config14.json` 기본 파라미터/ticker |
| GET | `/api/configs` | `auto_trader/config1–16.json` 목록 |
| GET | `/api/configs/{config_id}/params` | config `bear_bull_drop_buy_params` (`wma_period` → `period` 자동) |
| GET | `/api/tickers` | OHLCV 갭 보충 후 ticker별 `min_date` / `max_date` |
| POST | `/api/backtest` | body: `{ ticker, start, end, capital, params }` — 차트 MA는 `regime_ma_type`·`period`·`d_interval` 자동 |
| GET | `/api/backtest?config_id=...` | config1–16로 실행 |

백테스트는 평가 구간 앞 **300 영업일** 워밍업을 포함한 뒤 `run_backtest`에 넘기고, 차트/통계는 선택한 기간만 표시합니다.

## 구조

```
viewer/
  api/           FastAPI
  frontend/      React + lightweight-charts
  run_dev.sh
```
