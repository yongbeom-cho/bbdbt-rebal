# data_pipeline

## 스크립트 목록

| 파일 | 설명 |
|------|------|
| `01_get_ohlcv_data.py` | 실제 ETF/코인 OHLCV 데이터 수집 |
| `02_create_synthetic_leveraged_etf.py` | 합성 레버리지 ETF 생성 및 DB 저장 |

---

## 02_create_synthetic_leveraged_etf.py

### 개요

기존 ETF의 OHLCV 데이터를 기반으로 N배 레버리지 합성 ETF를 만들어 DB에 저장한다.

**생성 로직 (3x 예시):**

- 첫째 날 `open = 1.0`으로 시작
- 각 날의 OHLC는 당일 `lev_open` 기준으로 레버리지 배율만큼 증폭
- 다음 날 `open`은 전일 `open` 대비 다음 날 `open` 비율에 레버리지 적용

```
high[i]      = lev_open[i] × (1 + L × (soxx_high[i]  / soxx_open[i] - 1))
low[i]       = lev_open[i] × (1 + L × (soxx_low[i]   / soxx_open[i] - 1))
close[i]     = lev_open[i] × (1 + L × (soxx_close[i] / soxx_open[i] - 1))
lev_open[i+1] = lev_open[i] × (1 + L × (soxx_open[i+1] / soxx_open[i] - 1))
```

- `volume`은 원본 그대로 사용

### CLI 사용법

```bash
# LEV3SOXX 생성 (SOXX 3배)
conda run -n algo python sbin/data_pipeline/02_create_synthetic_leveraged_etf.py \
  --underlying SOXX --synthetic LEV3SOXX --leverage 3

# LEV3QQQ 생성 (QQQ 3배)
conda run -n algo python sbin/data_pipeline/02_create_synthetic_leveraged_etf.py \
  --underlying QQQ --synthetic LEV3QQQ --leverage 3

# 2배 레버리지
conda run -n algo python sbin/data_pipeline/02_create_synthetic_leveraged_etf.py \
  --underlying QQQ --synthetic LEV2QQQ --leverage 2
```

### Python에서 직접 호출

```python
from sbin.data_pipeline.create_synthetic_leveraged_etf import create_synthetic_leveraged_etf

create_synthetic_leveraged_etf(
    underlying_ticker="QQQ",
    synthetic_ticker="LEV3QQQ",
    leverage=3.0,
    db_path="var/data/usaetf_ohlcv_day.db",
)
```

### 옵션

| 옵션 | 기본값 | 설명 |
|------|--------|------|
| `--underlying` | (필수) | 원본 티커 (예: `SOXX`, `QQQ`) |
| `--synthetic` | (필수) | 생성할 티커명 (예: `LEV3SOXX`) |
| `--leverage` | `3.0` | 레버리지 배율 |
| `--db` | `var/data/usaetf_ohlcv_day.db` | SQLite DB 경로 |

### 현재 DB에 있는 합성 티커

| 티커 | 원본 | 레버리지 | bar 수 |
|------|------|----------|--------|
| LEV3SOXX | SOXX | 3x | 6,222 |
| LEV3QQQ  | QQQ  | 3x | 6,814 |

> 동일한 `--synthetic` 티커를 다시 실행하면 기존 데이터를 덮어쓴다.
