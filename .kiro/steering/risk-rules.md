---
inclusion: always
---

# Risk Management Rules

These rules are **non-negotiable**. They must never be bypassed, weakened, or hardcoded. All values come from `config.yaml` only.

## Position Sizing Formula (MUST follow exactly)

```
risk_amount = balance * risk_per_trade_pct / 100
sl_distance = entry_price * sl_pct / 100
position_size = risk_amount / sl_distance
```

This formula must be used for **every** trade. No exceptions. No alternative sizing methods.

## Risk Parameters (from config.yaml)

| Parameter | Config Key | Default | Description |
|---|---|---|---|
| Risk per trade | `risk.risk_per_trade_pct` | 1.0% | Percentage of balance risked per trade |
| Leverage | `risk.leverage` | 5x | Fixed leverage for all positions |
| Stop Loss | `strategy.exit.sl_pct` | 1.0% | Distance from entry price |
| TP1 | `strategy.exit.tp1_pct` | 0.8% | Take profit level 1 |
| TP2 | `strategy.exit.tp2_pct` | 1.5% | Take profit level 2 |
| TP3 | `strategy.exit.tp3_pct` | 2.5% | Take profit level 3 |
| TP1 close ratio | `strategy.exit.tp1_close_ratio` | 0.4 | Close 40% at TP1 |
| TP2 close ratio | `strategy.exit.tp2_close_ratio` | 0.4 | Close 40% at TP2 |
| TP3 trailing | `strategy.exit.trailing_stop_pct` | 0.5% | Trailing stop from high/low |
| Max hold time | `strategy.exit.max_hold_min` | 30 min | Force close after this |
| Max concurrent | `risk.max_concurrent_positions` | 3 | Max open positions |
| Daily loss halt | `risk.max_daily_loss_pct` | 3.0% | Halt bot if exceeded |
| Session drawdown halt | `risk.max_drawdown_pct` | 5.0% | Halt bot if exceeded |
| Min free margin | `risk.min_free_margin_pct` | 30.0% | Required before opening |

## Hard Rules (NEVER skip or change)

### 1. SL to Breakeven After TP1
When TP1 is hit:
- Close `tp1_close_ratio` of position
- **Move SL to entry price (breakeven)** — this is mandatory, not optional

### 2. Daily Loss Halt
When cumulative daily realized loss exceeds `max_daily_loss_pct`:
- **Immediately halt** — stop opening new positions
- Send Telegram alert with `⛔ HALT` message
- Existing positions continue to be managed (TP/SL still active)
- Bot does NOT close existing positions on halt — only stops new entries

### 3. Session Drawdown Halt
When session drawdown exceeds `max_drawdown_pct`:
- Same behavior as daily loss halt

### 4. Force Close at Max Hold Time
When a position has been open longer than `max_hold_min`:
- **Force close the entire remaining position** at market price
- No exceptions, regardless of current PnL or TP/SL state

### 5. Margin Check Before Every Order
Before opening any new position, ALL of these must pass:
- `daily_loss < max_daily_loss_pct` ✓
- `session_drawdown < max_drawdown_pct` ✓
- `open_positions < max_concurrent_positions` ✓
- `free_margin_pct >= min_free_margin_pct` ✓

If **any** check fails → reject the trade and log which condition failed.

### 6. Cooldown Per Symbol
After opening a position on a symbol:
- Suppress all entry signals for that symbol for `signal_cooldown_min` minutes
- Cooldown is per-symbol, not global

## Exit Strategy Flow

```
Position Opened
    │
    ├── Price hits TP1 (0.8%)
    │   ├── Close 40% of position
    │   └── Move SL → entry price (breakeven)
    │
    ├── Price hits TP2 (1.5%)
    │   └── Close 40% of original quantity
    │
    ├── Price hits TP3 (2.5%)
    │   └── Activate trailing stop (0.5% from high/low)
    │       └── Trailing stop triggered → close remaining 20%
    │
    ├── Price hits SL (1.0% or breakeven)
    │   └── Close entire remaining position
    │
    └── Time exceeds max_hold_min (30 min)
        └── Force close entire remaining position
```

## No Magic Numbers

- Every threshold, percentage, timeout, and limit must come from `config.yaml`
- Never write `0.008` when you mean `tp1_pct / 100`
- Never write `3` when you mean `max_concurrent_positions`
- Always reference the config object in code
