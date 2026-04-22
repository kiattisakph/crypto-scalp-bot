# crypto-scalp-bot — System Requirements
**Version**: 1.0.0  
**Status**: Draft  
**Project**: `crypto-scalp-bot`  
**Platform**: Binance USDT-M Perpetual Futures  
**Strategy**: Top Gainers Scalping (Dynamic Watchlist)

---

## 1. Project Overview

`crypto-scalp-bot` คือ automated trading bot สำหรับ Binance USDT-M Perpetual Futures โดยใช้ strategy **Top Gainers Scalping** — เลือกเทรดเฉพาะ top 5 symbols ที่มี 24h price change สูงสุด ณ ขณะนั้น แบบ real-time ผ่าน WebSocket และ execute scalping trades ด้วย multi-signal confirmation

### Goals
- ทำกำไรจาก momentum ของ symbols ที่กำลัง breakout
- ลด exposure โดยเลือกเฉพาะ symbols ที่มี volume + momentum จริง
- ระบบดูแลตัวเองได้ 24/7 บน Contabo VPS ผ่าน Docker

---

## 2. Tech Stack

| Layer | Technology |
|---|---|
| Language | Python 3.11 |
| Exchange SDK | `python-binance` / Binance REST + WebSocket |
| Indicators | `pandas_ta` |
| Data | `pandas`, `numpy` |
| Database | SQLite via `aiosqlite` |
| Alert | Telegram Bot API |
| Config | `pydantic-settings` + `.env` + `config.yaml` |
| Logging | `loguru` |
| Local dev | Python venv |
| Production | Docker + docker-compose |

---

## 3. Project Structure

```
crypto-scalp-bot/
├── main.py                         # Entry point — start bot
├── config.yaml                     # Strategy + risk parameters
├── .env                            # API keys (git-ignored)
├── .env.example                    # Template สำหรับ setup
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
│
├── core/
│   ├── __init__.py
│   ├── bot.py                      # BotEngine — orchestrate ทุก component
│   ├── config.py                   # Load + validate config (pydantic-settings)
│   └── enums.py                    # Signal, OrderSide, ExitReason enum
│
├── streams/
│   ├── __init__.py
│   ├── ticker_stream.py            # !ticker@arr WebSocket — market overview
│   └── kline_stream.py             # {symbol}@kline_3m/15m WebSocket per symbol
│
├── strategy/
│   ├── __init__.py
│   ├── watchlist_manager.py        # Dynamic top 5 symbol selection + rotation
│   ├── signal_engine.py            # Indicator calculation + entry/exit signals
│   └── top_gainers_scalping.py     # Strategy orchestrator — main logic
│
├── execution/
│   ├── __init__.py
│   ├── order_manager.py            # Place/cancel/modify orders via REST
│   └── position_manager.py        # Track open positions + TP/SL management
│
├── risk/
│   ├── __init__.py
│   └── risk_guard.py               # Portfolio-level guards + halt logic
│
├── storage/
│   ├── __init__.py
│   ├── database.py                 # SQLite connection + migrations
│   └── trade_repository.py        # CRUD สำหรับ trade history
│
├── notification/
│   ├── __init__.py
│   └── telegram_alert.py          # Telegram Bot alert sender
│
├── utils/
│   ├── __init__.py
│   ├── candle_buffer.py            # Rolling candle buffer per symbol/timeframe
│   └── time_utils.py              # Timezone helpers (UTC)
│
├── tests/
│   ├── test_signal_engine.py
│   ├── test_watchlist_manager.py
│   └── test_risk_guard.py
│
└── logs/                           # Loguru output (git-ignored)
    └── bot.log
```

---

## 4. Configuration

### 4.1 `.env`
```env
BINANCE_API_KEY=
BINANCE_API_SECRET=
BINANCE_TESTNET=false              # true = testnet, false = mainnet

TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

DB_PATH=./data/trades.db
LOG_LEVEL=INFO
```

### 4.2 `config.yaml`
```yaml
watchlist:
  top_n: 5                          # จำนวน symbols ที่ monitor
  min_change_pct_24h: 3.0           # ขั้นต่ำ 24h % change
  min_volume_usdt_24h: 10_000_000   # ขั้นต่ำ volume 24h
  refresh_interval_sec: 300         # re-rank ทุก 5 นาที
  max_concurrent_positions: 3       # เปิด position พร้อมกันสูงสุด
  blacklist:
    - "USDCUSDT"
    - "BUSDUSDT"
    - "BTCDOMUSDT"
  blacklist_patterns:
    - "UP"                          # leveraged token เช่น BTCUPUSDT
    - "DOWN"

strategy:
  signal_timeframe: "3m"
  trend_timeframe: "15m"
  candle_buffer_size: 100           # เก็บ candle ล่าสุดกี่แท่ง

  entry:
    rsi_period: 14
    rsi_long_min: 50
    rsi_long_max: 70
    rsi_short_min: 30
    rsi_short_max: 50
    ema_fast: 9
    ema_slow: 21
    ema_trend_fast: 20
    ema_trend_slow: 50
    volume_multiplier: 1.5          # volume ต้อง > 1.5x average
    resistance_buffer_pct: 0.3      # ห่างจาก resistance ≥ 0.3%
    signal_cooldown_min: 15         # cooldown ต่อ symbol หลัง entry

  exit:
    tp1_pct: 0.8
    tp2_pct: 1.5
    tp3_pct: 2.5
    tp1_close_ratio: 0.4            # ปิด 40% ที่ TP1
    tp2_close_ratio: 0.4            # ปิด 40% ที่ TP2
    trailing_stop_pct: 0.5          # trailing สำหรับ TP3
    sl_pct: 1.0
    max_hold_min: 30                # force close ถ้าเปิดนานเกินนี้

risk:
  risk_per_trade_pct: 1.0           # เสี่ยง 1% ของ balance ต่อ trade
  leverage: 5
  max_concurrent_positions: 3
  max_daily_loss_pct: 3.0           # halt ถ้า daily loss เกินนี้
  max_drawdown_pct: 5.0             # halt ถ้า session drawdown เกินนี้
  min_free_margin_pct: 30.0         # ต้องมี free margin ≥ 30% ก่อน open
```

---

## 5. Core Components

### 5.1 BotEngine (`core/bot.py`)
- Entry point หลักของระบบ
- Initialize ทุก component และ wire dependencies
- Start WebSocket streams ทั้งหมด
- Handle graceful shutdown (SIGTERM/SIGINT) → ปิด open positions ก่อน exit

```python
# Lifecycle
async def start():
    await database.init()
    await risk_guard.load_daily_state()
    await ticker_stream.connect()      # !ticker@arr
    await strategy.start()

async def stop():
    await strategy.close_all_positions()
    await ticker_stream.disconnect()
    await database.close()
```

### 5.2 WatchlistManager (`strategy/watchlist_manager.py`)
- Subscribe `!ticker@arr` WebSocket
- กรอง symbols ตาม filter rules ทุก tick
- Re-rank และอัพเดท top 5 ทุก `refresh_interval_sec`
- **Grace policy**: symbol ที่มี open position จะไม่ถูก drop ออกจาก watchlist จนกว่าจะปิด position
- Emit event `on_watchlist_changed(added: list, removed: list)` เมื่อ list เปลี่ยน

**Filter Rules (ทุกข้อต้องผ่าน)**:
```
1. symbol ลงท้ายด้วย "USDT"
2. ไม่อยู่ใน blacklist / blacklist_patterns
3. priceChangePercent (24h) ≥ min_change_pct_24h
4. quoteVolume (24h) ≥ min_volume_usdt_24h
5. lastPrice > 0.0001 USDT
→ Sort by priceChangePercent DESC → เลือก top N
```

### 5.3 KlineStream (`streams/kline_stream.py`)
- Dynamic subscribe/unsubscribe kline stream ตาม watchlist
- เมื่อ symbol เข้า watchlist → subscribe `{symbol}@kline_3m` + `{symbol}@kline_15m`
- เมื่อ symbol ออก watchlist → unsubscribe (ถ้าไม่มี open position)
- ส่ง closed candle เข้า `CandleBuffer` เมื่อ `x: true` (candle closed)

### 5.4 CandleBuffer (`utils/candle_buffer.py`)
- Rolling buffer เก็บ candle ล่าสุด N แท่ง ต่อ symbol ต่อ timeframe
- Thread-safe (asyncio)
- Return `pd.DataFrame` พร้อมสำหรับ `pandas_ta`

```python
buffer = CandleBuffer(max_size=100)
buffer.add(symbol="SOLUSDT", timeframe="3m", candle=candle_dict)
df = buffer.get_df(symbol="SOLUSDT", timeframe="3m")
# → DataFrame columns: open, high, low, close, volume, timestamp
```

### 5.5 SignalEngine (`strategy/signal_engine.py`)
- รับ DataFrame ของ 3m และ 15m
- คำนวณ indicators ทั้งหมดด้วย `pandas_ta`
- Return `Signal(direction, confidence, indicators_snapshot)`

**Indicators ที่คำนวณ**:
```python
# 15m — Trend Filter
df_15m.ta.ema(length=20, append=True)   # EMA_20
df_15m.ta.ema(length=50, append=True)   # EMA_50

# 3m — Entry Signal
df_3m.ta.ema(length=9, append=True)     # EMA_9
df_3m.ta.ema(length=21, append=True)    # EMA_21
df_3m.ta.rsi(length=14, append=True)    # RSI_14
# Volume MA (manual)
df_3m["vol_ma20"] = df_3m["volume"].rolling(20).mean()
```

**LONG Entry Logic (AND)**:
```
[15m] EMA_20 > EMA_50
[3m]  RSI_14 ∈ [50, 70]
[3m]  EMA_9 crossed above EMA_21 ในช่วง 2 candles ล่าสุด
[3m]  volume[-1] > vol_ma20[-1] * 1.5
[3m]  close[-1] > open[-1]  (bullish candle)
[3m]  close[-1] < resistance * (1 - 0.003)
```

**SHORT Entry Logic (AND)**:
```
[15m] EMA_20 < EMA_50
[3m]  RSI_14 ∈ [30, 50]
[3m]  EMA_9 crossed below EMA_21 ในช่วง 2 candles ล่าสุด
[3m]  volume[-1] > vol_ma20[-1] * 1.5
[3m]  close[-1] < open[-1]  (bearish candle)
[3m]  close[-1] > support * (1 + 0.003)
```

### 5.6 TopGainersScalping (`strategy/top_gainers_scalping.py`)
- Orchestrate ทุกอย่าง: รับ signal → check risk → place order
- Track cooldown per symbol
- จัดการ TP/SL levels per position
- Listen candle close events → re-evaluate signal

### 5.7 OrderManager (`execution/order_manager.py`)
- Wrapper รอบ Binance REST API
- Methods: `open_long()`, `open_short()`, `close_position()`, `set_leverage()`
- Handle Binance error codes + retry logic (max 3 retries)
- Testnet/Mainnet switching จาก config

### 5.8 PositionManager (`execution/position_manager.py`)
- Track open positions ใน memory
- Monitor TP1/TP2/TP3/SL hit ทุก tick (ผ่าน ticker price)
- Manage partial close สำหรับ multi-level TP
- Trigger force close เมื่อ max_hold_min ครบ
- Emit `on_position_closed(trade_result)` → บันทึก DB + alert

### 5.9 RiskGuard (`risk/risk_guard.py`)
- คำนวณ position size จาก formula:
  ```
  risk_amount = balance * risk_per_trade_pct / 100
  sl_distance = entry_price * sl_pct / 100
  position_size = risk_amount / sl_distance
  ```
- Check ก่อน open position ทุกครั้ง:
  - daily_loss < max_daily_loss_pct ✓
  - session_drawdown < max_drawdown_pct ✓
  - open_positions < max_concurrent_positions ✓
  - free_margin_pct ≥ min_free_margin_pct ✓
- Halt bot และ alert เมื่อ guard triggered

---

## 6. Database Schema (SQLite)

### `trades` table
```sql
CREATE TABLE trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,           -- LONG / SHORT
    entry_price     REAL NOT NULL,
    exit_price      REAL,
    quantity        REAL NOT NULL,
    leverage        INTEGER NOT NULL,
    pnl_usdt        REAL,
    pnl_pct         REAL,
    exit_reason     TEXT,                    -- TP1/TP2/TP3/SL/TIME/REVERSAL/HALT
    entry_at        DATETIME NOT NULL,
    exit_at         DATETIME,
    status          TEXT DEFAULT 'OPEN',     -- OPEN / CLOSED
    signal_snapshot TEXT,                    -- JSON: indicators at entry
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

### `daily_stats` table
```sql
CREATE TABLE daily_stats (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            TEXT NOT NULL UNIQUE,    -- YYYY-MM-DD
    starting_balance REAL,
    ending_balance  REAL,
    total_trades    INTEGER DEFAULT 0,
    winning_trades  INTEGER DEFAULT 0,
    total_pnl_usdt  REAL DEFAULT 0,
    max_drawdown_pct REAL DEFAULT 0,
    halted          INTEGER DEFAULT 0,       -- 0/1
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

---

## 7. Notification (Telegram)

### Events ที่ส่ง alert

| Event | Message |
|---|---|
| Bot started | `🟢 crypto-scalp-bot started` |
| Bot stopped | `🔴 Bot stopped` |
| Watchlist changed | `📋 Watchlist: +SOLUSDT -LINKUSDT` |
| Position opened | `📈 LONG SOLUSDT @$145.20 \| Size: 0.5 \| SL: $143.75 \| TP1: $146.36` |
| TP1 hit | `✅ TP1 SOLUSDT +0.8% \| PnL: +$4.00` |
| TP2 hit | `✅ TP2 SOLUSDT +1.5% \| PnL: +$7.50` |
| Position closed | `🏁 CLOSED SOLUSDT \| Reason: TP3 \| PnL: +$12.30` |
| SL hit | `🛑 SL SOLUSDT -1.0% \| PnL: -$5.00` |
| Risk halt | `⛔ HALT — Daily loss limit reached (-3%)` |
| Reconnect | `⚠️ WebSocket reconnected after 45s` |

---

## 8. WebSocket Streams

### 8.1 Streams ที่ใช้

| Stream | URL | ใช้เพื่อ |
|---|---|---|
| All Tickers | `wss://fstream.binance.com/ws/!ticker@arr` | WatchlistManager |
| Kline 3m | `wss://fstream.binance.com/ws/{symbol}@kline_3m` | Signal (per symbol) |
| Kline 15m | `wss://fstream.binance.com/ws/{symbol}@kline_15m` | Trend filter (per symbol) |

### 8.2 Reconnection Policy
```
Backoff: 1s → 2s → 4s → 8s → 16s → 30s (max)
ถ้า disconnect > 60s → close all open positions → alert → retry
ถ้า reconnect สำเร็จ → re-subscribe streams ทั้งหมด → alert
```

---

## 9. Docker Setup

### `Dockerfile`
```dockerfile
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "main.py"]
```

### `docker-compose.yml`
```yaml
version: "3.8"
services:
  bot:
    build: .
    container_name: crypto-scalp-bot
    restart: unless-stopped
    env_file: .env
    volumes:
      - ./data:/app/data          # SQLite database
      - ./logs:/app/logs          # Log files
      - ./config.yaml:/app/config.yaml
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "5"
```

### Commands
```bash
# Local dev (venv)
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python main.py

# Production (Docker)
docker-compose up -d
docker-compose logs -f bot
docker-compose down
```

---

## 10. Logging

ใช้ `loguru` output ทั้ง console และ file

```
logs/
├── bot.log          # rotation ทุก 10MB, เก็บ 7 วัน
└── trades.log       # เฉพาะ trade events
```

**Log format**:
```
2026-04-22 14:32:01 | INFO | watchlist | Watchlist updated: ['SOLUSDT', 'SUIUSDT', 'PEPEUSDT', 'WIFUSDT', 'BONKUSDT']
2026-04-22 14:33:15 | INFO | signal | LONG signal: SOLUSDT | RSI=58.3 | EMA9>EMA21 | Vol=2.1x
2026-04-22 14:33:16 | INFO | order | Opened LONG SOLUSDT | entry=145.20 | qty=0.50 | SL=143.75
2026-04-22 14:38:42 | INFO | position | TP1 hit SOLUSDT | exit=146.36 | pnl=+4.00 USDT
```

---

## 11. Risk Management Summary

| Rule | Value |
|---|---|
| Risk per trade | 1% of balance |
| Leverage | 5x fixed |
| Stop Loss | 1.0% from entry |
| TP1 / TP2 / TP3 | 0.8% / 1.5% / 2.5% |
| TP1 close ratio | 40% |
| TP2 close ratio | 40% |
| TP3 trailing | 0.5% from high |
| Breakeven move | SL → entry after TP1 |
| Max hold time | 30 min |
| Max concurrent positions | 3 |
| Daily loss halt | -3% of starting balance |
| Session drawdown halt | -5% |
| Min free margin | 30% |

---

## 12. Acceptance Criteria

| # | Criteria |
|---|---|
| AC-01 | Bot start ได้ทั้ง local venv และ Docker container |
| AC-02 | WatchlistManager อัพเดท top 5 symbols ทุก 5 นาที โดยไม่กระทบ open positions |
| AC-03 | Entry signal ต้องผ่านทุก condition ใน section 5.5 ถึงจะ place order |
| AC-04 | SL ย้ายมา breakeven อัตโนมัติเมื่อ TP1 hit |
| AC-05 | Position ถูก force close เมื่อเปิดนานเกิน 30 นาที |
| AC-06 | Bot halt อัตโนมัติเมื่อ daily loss > 3% พร้อม Telegram alert |
| AC-07 | WebSocket reconnect ได้ภายใน 30 วินาที หากหลุดนานเกิน 60 วินาที ปิด position ทั้งหมด |
| AC-08 | Position size คำนวณจาก formula section 5.9 ทุกครั้ง |
| AC-09 | ทุก trade บันทึกลง SQLite ครบถ้วน |
| AC-10 | Telegram alert ส่งทุก event ใน section 7 |
| AC-11 | Testnet mode สลับได้จาก `.env` โดยไม่ต้องแก้ code |
| AC-12 | Graceful shutdown ปิด open positions ก่อน exit เสมอ |

---

## 13. Out of Scope (v1)

- ❌ Web dashboard / UI
- ❌ Backtesting engine
- ❌ SHORT selling (v1 เน้น LONG only)
- ❌ Multi-exchange support
- ❌ ML-based signal
- ❌ Funding rate optimization
- ❌ Hedging logic
