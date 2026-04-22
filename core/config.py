"""Configuration loading and validation for crypto-scalp-bot.

Loads environment variables from `.env` via pydantic-settings and
strategy/risk parameters from `config.yaml` via pydantic models.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml
from loguru import logger
from pydantic import BaseModel, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# ---------------------------------------------------------------------------
# Environment settings (loaded from .env)
# ---------------------------------------------------------------------------

class EnvSettings(BaseSettings):
    """Settings loaded from the `.env` file."""

    binance_api_key: str
    binance_api_secret: str
    binance_demo: bool = False
    telegram_bot_token: str
    telegram_chat_id: str
    db_path: str = "./data/trades.db"
    log_level: str = "INFO"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


# ---------------------------------------------------------------------------
# Application config models (loaded from config.yaml)
# ---------------------------------------------------------------------------

class WatchlistConfig(BaseModel):
    """Watchlist filtering and ranking parameters."""

    top_n: int = 5
    min_change_pct_24h: float = 3.0
    min_volume_usdt_24h: float = 10_000_000
    refresh_interval_sec: int = 300
    max_concurrent_positions: int = 3
    blacklist: list[str] = []
    blacklist_patterns: list[str] = []

    @field_validator("top_n", "max_concurrent_positions")
    @classmethod
    def _positive_int(cls, v: int, info) -> int:
        if v <= 0:
            raise ValueError(f"{info.field_name} must be > 0, got {v}")
        return v

    @field_validator("min_change_pct_24h", "min_volume_usdt_24h")
    @classmethod
    def _positive_float(cls, v: float, info) -> float:
        if v <= 0:
            raise ValueError(f"{info.field_name} must be > 0, got {v}")
        return v

    @field_validator("refresh_interval_sec")
    @classmethod
    def _positive_interval(cls, v: int) -> int:
        if v <= 0:
            raise ValueError(f"refresh_interval_sec must be > 0, got {v}")
        return v


class EntryConfig(BaseModel):
    """Signal entry indicator parameters."""

    rsi_period: int = 14
    rsi_long_min: float = 50
    rsi_long_max: float = 70
    rsi_short_min: float = 30
    rsi_short_max: float = 50
    ema_fast: int = 9
    ema_slow: int = 21
    ema_trend_fast: int = 20
    ema_trend_slow: int = 50
    atr_period: int = 14
    adx_period: int = 14
    adx_trend_threshold: float = 20.0
    volume_multiplier: float = 1.5
    resistance_buffer_pct: float = 0.3
    signal_cooldown_min: int = 15

    # Funding rate filter
    max_funding_rate_pct: float = 0.05
    reject_funding_against_position: bool = True

    @field_validator("rsi_period", "atr_period", "adx_period")
    @classmethod
    def _period_positive(cls, v: int, info) -> int:
        if v <= 0:
            raise ValueError(f"{info.field_name} must be > 0, got {v}")
        return v

    @field_validator("rsi_long_min", "rsi_long_max", "rsi_short_min", "rsi_short_max", "adx_trend_threshold")
    @classmethod
    def _rsi_bounds(cls, v: float, info) -> float:
        if not 0 < v <= 100:
            raise ValueError(f"{info.field_name} must be between 0 (exclusive) and 100, got {v}")
        return v

    @field_validator("ema_fast", "ema_slow", "ema_trend_fast", "ema_trend_slow")
    @classmethod
    def _ema_positive(cls, v: int, info) -> int:
        if v <= 0:
            raise ValueError(f"{info.field_name} must be > 0, got {v}")
        return v

    @field_validator("volume_multiplier")
    @classmethod
    def _volume_multiplier_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"volume_multiplier must be > 0, got {v}")
        return v

    @field_validator("resistance_buffer_pct")
    @classmethod
    def _resistance_buffer_non_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError(f"resistance_buffer_pct must be >= 0, got {v}")
        return v

    @field_validator("signal_cooldown_min")
    @classmethod
    def _cooldown_non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"signal_cooldown_min must be >= 0, got {v}")
        return v

    @field_validator("max_funding_rate_pct")
    @classmethod
    def _max_funding_non_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError(f"max_funding_rate_pct must be >= 0, got {v}")
        return v

    @model_validator(mode="after")
    def _check_rsi_ranges(self) -> EntryConfig:
        if self.rsi_long_min >= self.rsi_long_max:
            raise ValueError(
                f"rsi_long_min ({self.rsi_long_min}) must be < rsi_long_max ({self.rsi_long_max})"
            )
        if self.rsi_short_min >= self.rsi_short_max:
            raise ValueError(
                f"rsi_short_min ({self.rsi_short_min}) must be < rsi_short_max ({self.rsi_short_max})"
            )
        return self

    @model_validator(mode="after")
    def _check_ema_pairs(self) -> EntryConfig:
        if self.ema_fast >= self.ema_slow:
            raise ValueError(
                f"ema_fast ({self.ema_fast}) must be < ema_slow ({self.ema_slow})"
            )
        if self.ema_trend_fast >= self.ema_trend_slow:
            raise ValueError(
                f"ema_trend_fast ({self.ema_trend_fast}) must be < ema_trend_slow ({self.ema_trend_slow})"
            )
        return self


class ExitConfig(BaseModel):
    """Position exit / take-profit / stop-loss parameters.

    Supports two modes:
    - **Fixed-percentage** (legacy): TP/SL set as flat % from entry price.
      Used when ``atr_mode = false``.
    - **ATR-based** (recommended): TP/SL set as multiples of the
      3-minute Average True Range at entry. Automatically scales to
      each coin's volatility — tight for BTC, wide for meme coins.
      Enabled when ``atr_mode = true``.
    """

    tp1_pct: float = 0.8
    tp2_pct: float = 1.5
    tp3_pct: float = 2.5
    tp1_close_ratio: float = 0.4
    tp2_close_ratio: float = 0.4
    trailing_stop_pct: float = 0.5
    sl_pct: float = 1.0
    max_hold_min: int = 30

    # ATR-based TP/SL multipliers (optional, overrides pct when atr_mode=True)
    atr_mode: bool = True
    atr_tp1_mult: float = 0.8
    atr_tp2_mult: float = 1.5
    atr_tp3_mult: float = 2.5
    atr_sl_mult: float = 1.0
    atr_trailing_mult: float = 0.5

    @field_validator("tp1_pct", "tp2_pct", "tp3_pct", "trailing_stop_pct", "sl_pct")
    @classmethod
    def _pct_positive(cls, v: float, info) -> float:
        if v <= 0:
            raise ValueError(f"{info.field_name} must be > 0, got {v}")
        return v

    @field_validator("atr_tp1_mult", "atr_tp2_mult", "atr_tp3_mult", "atr_trailing_mult", "atr_sl_mult")
    @classmethod
    def _atr_mult_positive(cls, v: float, info) -> float:
        if v <= 0:
            raise ValueError(f"{info.field_name} must be > 0, got {v}")
        return v

    @field_validator("tp1_close_ratio", "tp2_close_ratio")
    @classmethod
    def _ratio_in_range(cls, v: float, info) -> float:
        if not 0 < v < 1:
            raise ValueError(f"{info.field_name} must be between 0 and 1 exclusive, got {v}")
        return v

    @field_validator("max_hold_min")
    @classmethod
    def _max_hold_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError(f"max_hold_min must be > 0, got {v}")
        return v

    @model_validator(mode="after")
    def _check_tp_ordering(self) -> ExitConfig:
        if not (self.tp1_pct < self.tp2_pct < self.tp3_pct):
            raise ValueError(
                f"Take-profit levels must be ascending: "
                f"tp1_pct ({self.tp1_pct}) < tp2_pct ({self.tp2_pct}) < tp3_pct ({self.tp3_pct})"
            )
        return self

    @model_validator(mode="after")
    def _check_atr_tp_ordering(self) -> ExitConfig:
        if not (self.atr_tp1_mult < self.atr_tp2_mult < self.atr_tp3_mult):
            raise ValueError(
                f"ATR take-profit multipliers must be ascending: "
                f"atr_tp1_mult ({self.atr_tp1_mult}) < atr_tp2_mult ({self.atr_tp2_mult}) "
                f"< atr_tp3_mult ({self.atr_tp3_mult})"
            )
        return self

    @model_validator(mode="after")
    def _check_close_ratios_sum(self) -> ExitConfig:
        total = self.tp1_close_ratio + self.tp2_close_ratio
        if total >= 1.0:
            raise ValueError(
                f"tp1_close_ratio + tp2_close_ratio must be < 1.0 "
                f"(need remaining quantity for TP3/trailing), got {total}"
            )
        return self


class RiskConfig(BaseModel):
    """Portfolio-level risk management parameters."""

    risk_per_trade_pct: float = 1.0
    leverage: int = 5
    max_concurrent_positions: int = 3
    max_daily_loss_pct: float = 3.0
    max_drawdown_pct: float = 5.0
    min_free_margin_pct: float = 30.0
    reconciliation_interval_sec: int = 30

    @field_validator("risk_per_trade_pct", "max_daily_loss_pct", "max_drawdown_pct")
    @classmethod
    def _pct_positive(cls, v: float, info) -> float:
        if v <= 0:
            raise ValueError(f"{info.field_name} must be > 0, got {v}")
        return v

    @field_validator("leverage")
    @classmethod
    def _leverage_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError(f"leverage must be > 0, got {v}")
        return v

    @field_validator("max_concurrent_positions")
    @classmethod
    def _max_positions_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError(f"max_concurrent_positions must be > 0, got {v}")
        return v

    @field_validator("min_free_margin_pct")
    @classmethod
    def _margin_in_range(cls, v: float) -> float:
        if not 0 < v <= 100:
            raise ValueError(f"min_free_margin_pct must be between 0 (exclusive) and 100, got {v}")
        return v

    @field_validator("reconciliation_interval_sec")
    @classmethod
    def _reconciliation_interval_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError(f"reconciliation_interval_sec must be > 0, got {v}")
        return v


class StrategyConfig(BaseModel):
    """Strategy-level configuration grouping entry and exit configs."""

    signal_timeframe: str = "3m"
    trend_timeframe: str = "15m"
    candle_buffer_size: int = 100
    entry: EntryConfig = EntryConfig()
    exit: ExitConfig = ExitConfig()

    @field_validator("candle_buffer_size")
    @classmethod
    def _buffer_size_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError(f"candle_buffer_size must be > 0, got {v}")
        return v

    @field_validator("signal_timeframe", "trend_timeframe")
    @classmethod
    def _timeframe_not_empty(cls, v: str, info) -> str:
        if not v.strip():
            raise ValueError(f"{info.field_name} must not be empty")
        return v


class AppConfig(BaseModel):
    """Top-level application configuration loaded from config.yaml."""

    watchlist: WatchlistConfig
    strategy: StrategyConfig
    risk: RiskConfig


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_config(
    env_file: str | Path = ".env",
    config_file: str | Path = "config.yaml",
) -> tuple[EnvSettings, AppConfig]:
    """Load and validate configuration from `.env` and `config.yaml`.

    Args:
        env_file: Path to the `.env` file.
        config_file: Path to the `config.yaml` file.

    Returns:
        A tuple of (EnvSettings, AppConfig).

    Raises:
        SystemExit: If any configuration validation fails.
    """
    # --- Load environment settings ---
    try:
        env = EnvSettings(_env_file=env_file)
    except ValidationError as exc:
        logger.error("config | .env validation failed:\n{errors}", errors=exc)
        sys.exit(1)

    # --- Load YAML application config ---
    config_path = Path(config_file)
    if not config_path.exists():
        logger.error("config | config.yaml not found at {path}", path=config_path)
        sys.exit(1)

    try:
        with open(config_path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        logger.error("config | Failed to parse config.yaml: {err}", err=exc)
        sys.exit(1)

    if not isinstance(raw, dict):
        logger.error("config | config.yaml must be a YAML mapping, got {t}", t=type(raw).__name__)
        sys.exit(1)

    try:
        app_config = AppConfig(**raw)
    except ValidationError as exc:
        logger.error("config | config.yaml validation failed:\n{errors}", errors=exc)
        sys.exit(1)

    return env, app_config
