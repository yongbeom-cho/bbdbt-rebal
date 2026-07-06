# bbdbt-rebal · auto_trader

미국 ETF **Bear-Bull Drop-Buy** 전략을 한국투자증권 해외주식 API로 장마감 직전에 실행하는 라이브 트레이더입니다.  
`bbdbt-rebal` 프로젝트는 동일 전략 위에 **드리프트 기반 cash-only 리밸런싱**을 추가한 버전입니다.

---

## 빠른 실행

```bash
# repo 루트 — 기본 config.json (config60 + drift 34%)
bash auto_trader/run_auto_trade.sh

# drift 프로파일 선택
CONFIG=config3.json bash auto_trader/run_auto_trade.sh
bash auto_trader/run_auto_trade.sh --config config2.json

# dry-run (주문 API 호출 없음)
bash auto_trader/run_auto_trade.sh --dry-run

# Windows (Git Bash) — 23:51 KST마다 실행
bash auto_trader/run_auto_trade_win.sh
```

`run_auto_trade.sh` / `run_auto_trade_win.sh` → `auto_trade.py --config …` (기본 `config.json`)

### Windows 새 PC 셋업

1. Git Bash + Python 3.9+ (또는 `.venv`)
2. `pip install -r auto_trader/requirements-auto-trader.txt`
3. `auto_trader/.env` 작성 (`app_key`, `app_secret`, `acc_no`, `discord_url`)
4. `bash auto_trader/run_auto_trade_win.sh`

`auto_trade.py` 가 장마감 10분 전까지 대기하므로, win 스크립트는 **23:51 KST** 에 프로세스만 깨워줍니다.

---

## Config 프로파일 (drift 포함)

SOXL + SHNY + TQQQ 3티커. **`rebal` 블록**이 config에 포함되어 있으며 `auto_trade.py` 가 EOD에 cash-only 리밸을 실행합니다.

| Config | 전략 베이스 | drift | 기대 CAGR/MDD | 용도 |
|--------|------------|-------|---------------|------|
| **`config.json`** | config60 | **34%** | 39.7% / 40.9% | **기본** (`auto_trade.py` default) |
| `config1.json` | config60 | **34%** | 39.7% / 40.9% | config.json 과 동일 프로파일 |
| `config2.json` | config60 | **29%** | 38.7% / 42.5% | |
| `config3.json` | config60 | **11%** | 35.0% / 39.1% | 잦은 rebal |
| `config4.json` | config40 | **12%** | — | config40 파라미터 |

config JSON 예 (`rebal` 블록):

```json
"rebal": {
  "enabled": true,
  "threshold": 0.34,
  "cooldown_sessions": 20
}
```

`threshold` 는 `0.34` 또는 `34` (%) 모두 가능.

레거시: `config40.json`, `config60.json` — drift 없음, 파라미터 참조용.

---

## 운용 티커

티커는 **`strategies` 배열**로 정의. config1~4 모두 SOXL, SHNY, TQQQ.

---

## 환경 설정

| 항목 | 위치 | 설명 |
|------|------|------|
| API 키 | `auto_trader/.env` 또는 config | `app_key`, `app_secret`, `acc_no`, `api_url` |
| Discord | `.env` | `discord_url` 또는 `DISCORD_WEBHOOK_URL` |
| 브로커 토큰 | `auto_trader/token.dat` | API 토큰 캐시 (만료 시 자동 재발급) |

---

## 일일 실행 흐름 (`auto_trade.py`)

미국 동부(ET) 기준 **정규장 마감 10분 전** (`minutes_before_close`, 기본 10) 윈도우에 진입한 뒤, 티커별로 매도 → 매수를 순서대로 처리합니다.

```
┌─────────────────────────────────────────────────────────────┐
│ 1. position_state_multi.json 로드                           │
│ 2. 브로커 잔고/포지션과 로컬 lot·cash 동기화 (초기 sync)     │
│ 3. 장마감 N분 전까지 sleep (pre_close_sleep_seconds=60)     │
│ 4. OHLCV need_len(200)봉 fetch → bull/bear 판정             │
│ 5. 티커별 매도 페이즈 (TP / surge partial) → 실주문       │
│ 6. 티커별 매수 페이즈 (down-day drop buy) → 실주문         │
│ 7. EOD drift rebal (config.rebal, cash-only)              │
│ 8. 최종 sync → state + fills + rebal_events 저장          │
└─────────────────────────────────────────────────────────────┘
```

주말(`weekday >= 5`)은 기본 스킵 (`allow_weekend_run` 없으면).

같은 `last_session_date` 가 이미 저장돼 있으면 해당 티커는 스킵 (`--force` 로 무시).

---

## 디스크에 저장되는 메타 / 상태

### 1. `position_state_multi.json` (기본 `--state`)

매일 세션 종료 후 덮어씁니다. 멀티 티커 포트폴리오의 **권위 있는 로컬 상태**입니다.

```json
{
  "version": 4,
  "saved_at_utc": "2026-07-06T04:55:00+00:00",
  "strategies": {
    "SOXL": {
      "cash_usd": 12345.67890123,
      "cash": 12345.67890123,
      "last_session_date": "2026-07-05",
      "next_lot_seq": 4,
      "lots": [
        {
          "lot_id": "20260705-001",
          "ticker": "SOXL",
          "shares": 12.3456789012,
          "entry_price": 45.12345678,
          "opened_session_date": "2026-07-05",
          "opened_at_utc": "2026-07-05T20:55:00+00:00",
          "buy_reason": "day_drop",
          "invested_usd": 557.12345678
        }
      ]
    },
    "TQQQ": { "...": "..." }
  }
}
```

| 필드 | 의미 |
|------|------|
| `cash` / `cash_usd` | 해당 티커 전략에 할당된 **미투자 현금** (브로커 총현금을 티커 수로 논리 분배) |
| `lots[]` | drop-buy로 쌓인 **lot 단위** 보유 (주수·진입가·lot_id) |
| `next_lot_seq` | 당일 lot_id 시퀀스 (`YYYYMMDD-NNN`) |
| `last_session_date` | 마지막 처리된 미국 거래일 (중복 실행 방지) |

정밀도: `shares` 10자리, `entry_price`/`cash` 8자리 (장기 백테스트·live replay parity용).

### 2. `fills.jsonl` (기본 `--fills`)

체결(또는 dry-run 시뮬 체결) 1건당 JSON 한 줄 append.

```json
{
  "side": "buy",
  "ticker": "SOXL",
  "tx_date": "2026-07-05",
  "filled_at_utc": "2026-07-05T20:55:12+00:00",
  "signal_close_px": 45.12,
  "quantity_shares": 10.0,
  "avg_fill_price": 45.12,
  "gross_usd": 451.2,
  "reason": "day_drop",
  "lot_id": "20260705-002",
  "order_chunk_usd": 500.0
}
```

| `reason` 값 | 의미 |
|-------------|------|
| `drop_take_profit_lot` | lot별 익절 매도 |
| `drop_surge_partial_newest` | 급등일 최신 lot 부분 매도 |
| `day_drop` | 하락일 drop-buy 매수 |

### 3. `rebal_events.jsonl` (기본 `--rebal-events`)

리밸 발생 시 append. 브로커 주문 없음 (cash-only 논리 이동).

```json
{
  "date": "2026-07-05",
  "weights_before": {"SOXL": 0.45, "SHNY": 0.20, "TQQQ": 0.35},
  "weights_after": {"SOXL": 0.33, "SHNY": 0.33, "TQQQ": 0.34},
  "transfers": {"SOXL": -5000, "SHNY": 2500, "TQQQ": 2500},
  "cash_moved": 5000,
  "threshold": 0.34
}
```

### 4. 기타 런타임 파일

| 파일 | 설명 |
|------|------|
| `token.dat` | 한국투자 API OAuth 토큰 |
| Discord webhook | 체결·경고·스킵 알림 (파일 저장 아님) |

### 5. Live rebal backtest 전용 (`live_rebal_engine`)

`bash auto_trader/run_live_rebal_backtest.sh` 실행 시 `var/live_rebal/{run_name}/` 아래:

| 파일 | 설명 |
|------|------|
| `position_state.json` | 실거래와 동일 스키마 + **`rebal` 블록** |
| `meta/equity_log.jsonl` | 일별 포트폴리오·티커별 equity |
| `meta/rebal_events.jsonl` | 리밸 발생 시 weights/transfers |
| `summary.json` | CAGR/MDD/rebal 횟수 요약 |

`position_state.json` 의 rebal 블록 예:

```json
"rebal": {
  "last_rebal_date": "2021-03-02",
  "cooldown_sessions_remaining": 0
},
"run_meta": {
  "tickers": ["LEV3GOLD", "LEV3NASDAQ", "LEV3SOX"],
  "rebal_threshold": 0.34,
  "rebal_cooldown": 20
}
```

---

## Bear-Bull Drop-Buy 전략 알고리즘

백테스트(`bear_bull_drop_buy.backtest`)와 동일한 **bar 단위** 로직입니다. 구현: `drop_buy_live.py`.

### Bull / Bear 판정

- OHLCV 최근 `ohlcv_bars`(기본 200)봉 슬라이스 사용
- `regime_ma_type`(wma/sma/ema) + `d_interval` + `period` 로 **서브샘플 MA 기울기** 계산
- 기울기 > 0 → **bull**, 아니면 **bear**
- bull/bear 에 따라 아래 파라미터 세트가 전환됩니다.

| 파라미터 | bull | bear | 역할 |
|----------|------|------|------|
| `*_take_profit_pct` | `bull_take_profit_pct` | `bear_take_profit_pct` | lot 익절 % |
| `*_day_drop_buy_pct` | `bull_day_drop_buy_pct` | `bear_day_drop_buy_pct` | 매수 트리거 하락률 |
| `*_equity_buy_frac` | `bull_equity_buy_frac` | `bear_equity_buy_frac` | sizing equity 대비 매수 비율 |
| `*_day_surge_partial_exit_pct` | bull | bear | 급등일 부분매도 트리거 |
| `*_day_surge_sell_newest_n` | bull | bear | 급등일 매도 lot 수 (최신부터) |

### 세션당 처리 순서 (티커별)

**매도 페이즈** (`apply_drop_buy_sell_phase`)

1. **Lot TP**: `close >= entry × (1 + tp_pct)` 인 lot을 앞에서부터 전량 매도
2. **Surge partial**: `day_ret >= surge_pct` 이면 최신 lot N개 전량 매도

**매수 페이즈** (`apply_drop_buy_buy_phase`)

3. **Drop buy**: `day_ret <= -day_drop_pct` 이면  
   `spend = equity_buy_frac × total_equity(cash, lots, close)` 만큼 매수  
   (`total_equity = cash + Σ shares×close`)

### 실거래와 시뮬의 차이

| 항목 | 백테스트 | live (`auto_trade.py`) |
|------|----------|------------------------|
| 주문 | close 가격 즉시 체결 | 시장가 limit, `chunk_limit_usd`(50k) 분할 |
| 주수 | fractional | **정수 주** (1주 잔돈은 직전 lot에 merge) |
| 체결가 | close | 브로커 평단 역산 |
| sync | 없음 | 매 fill 전후 브로커 잔고↔로컬 lot 동기화 |

commission/slippage: config `strategy_params` (기본 0.25% / 0.1%).

---

## Rebalance 알고리즘

구현: `rebal/rebal_logic.py`  
실거래: `auto_trade.py` EOD (config `rebal` 블록)  
백테스트: `rebal/backtest_rebal.py`  
replay: `auto_trader/live_rebal_engine.py`

### 트리거 (Drift)

N개 티커 균등 목표 비중 `1/N`.

```
weight[t] = equity[t] / Σ equity
max_drift = max |weight[t] - 1/N|

if max_drift >= rebal_threshold  →  리밸 실행
```

- `equity[t] = cash[t] + Σ(lot.shares × close[t])`
- 기본 threshold: **34%** (config60 + LEV3 3티커 백테스트 기준)
- **Cooldown**: 리밸 후 **20거래일** 동안 재트리거 안 함

### 실행 (Cash-only)

**주식 추가 매도 없음.** lot은 그대로 두고 **전략 현금만** 이동합니다.

```
excess[t] = equity[t] - total/N

Giver  (excess > 0):  cash[t] 에서 min(excess, cash[t]) 만큼 회수
Receiver (excess < 0): 회수 pool 을 deficit 비율로 분배 → cash[t] 에 입금
```

Giver의 가용 cash가 부족하면 그만큼만 이동 (lot 매도로 강제 충당하지 않음).

### 예시 (3티커, threshold 34%)

| 날짜 | 이벤트 |
|------|--------|
| 1995-07-24 | 1차 rebal |
| 1997-08-19 | 2차 |
| 2000-10-19 | 3차 |
| 2008-01-08 | 4차 |
| 2011-08-05 | 5차 |
| 2021-03-02 | 6차 |

config60 · LEV3GOLD+LEV3NASDAQ+LEV3SOX · drift 34% · 1994~2025 기준:  
**CAGR 39.8% / MDD 40.9% / rebal 6회**

### Live replay vs batch backtest

| | `backtest_rebal.py` | `live_rebal_engine` |
|--|---------------------|----------------------|
| state | 메모리 only | 매일 JSON load/save |
| 신호 | full OHLCV bull flag | 동일 (full df precompute) |
| rebal | EOD | EOD, `rebal_events.jsonl` 기록 |
| parity | — | batch와 **동일** (state 정밀도 수정 후) |

검증:

```bash
python3 sbin/check_live_parity_rebal.py \
  --config auto_trader/config60.json \
  --tickers LEV3GOLD LEV3NASDAQ LEV3SOX \
  --period 1994-05-04:2025-12-31 \
  --rebal-threshold 0.34
```

---

## Config 프로파일 요약

config1~4 — 위 표 참조. `config40.json` / `config60.json` 은 drift 없는 파라미터 레퍼런스.

---

## 브로커 ↔ 로컬 상태 동기화

실거래에서 로컬 lot/cash가 브로커와 어긋나지 않도록 3단계 sync:

1. **Lot sync** (`sync_tracked_with_broker`)  
   - 브로커 보유 수량·평단 vs 로컬 `lots[]`  
   - 최근 `fills.jsonl` 4건으로 2^N 롤백 시나리오 시도 후 수량 맞춤  
   - 실패 시 tail lot 강제 trim/add

2. **Cash redistribution** (`sync_local_cash_and_position_info_with_broker`)  
   - 브로커 총현금 vs Σ `cash_by_ticker` 차이를 티커별로 균등 가감

3. **Consistency check**  
   - fill 전후 lot notional Δ vs cash Δ 가 5% 이상 어긋나면 Discord 경고

---

## 관련 스크립트

| 스크립트 | 용도 |
|----------|------|
| `run_auto_trade.sh` | **실거래** (기본 `config.json`) |
| `run_live_rebal_backtest.sh` | state/meta 파일 생성하며 history replay |
| `live_rebal_backtest.py replay` | 위 wrapper의 Python 진입점 |
| `../rebal/backtest_rebal.py` | batch multi-ticker rebal 백테스트 |
| `../sbin/check_live_parity_rebal.py` | batch vs live replay parity |

---

## 파일 구조

```
auto_trader/
├── README.md                 ← 이 문서
├── run_auto_trade.sh         ← 실거래 진입 (기본 config.json)
├── run_auto_trade_win.sh     ← Windows Git Bash loop
├── auto_trade.py             ← 메인 live loop
├── drop_buy_live.py          ← sell/buy phase (backtest 동형)
├── position_state.py         ← state JSON 스키마
├── broker_flow.py            ← OHLCV·주문·잔고
├── koreainvestment.py        ← KIS API 래퍼
├── live_rebal_engine.py      ← rebal + daily state replay
├── live_rebal_backtest.py    ← replay CLI
├── run_live_rebal_backtest.sh
├── config.json               ← 기본 live config
├── config1.json … config4.json
├── config40.json / config60.json
├── position_state_multi.json ← 런타임 생성
├── fills.jsonl               ← 런타임 생성
└── rebal_events.jsonl        ← 런타임 생성
```
