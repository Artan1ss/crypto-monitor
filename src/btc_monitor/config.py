from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "BTC Signal Monitor V1"
    environment: str = "development"
    log_level: str = "INFO"
    database_url: str = "postgresql+asyncpg://postgres:postgres@127.0.0.1:5433/btc_monitor"

    bybit_rest_url: str = "https://api.bybit.com"
    bybit_spot_ws_url: str = "wss://stream.bybit.com/v5/public/spot"
    bybit_linear_ws_url: str = "wss://stream.bybit.com/v5/public/linear"
    symbol: str = "BTCUSDT"

    telegram_enabled: bool = False
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    send_startup_message: bool = False
    orderbook_enabled: bool = False

    cvd_warmup_bars: int = Field(12, ge=1)
    pivot_left: int = Field(3, ge=1)
    pivot_right: int = Field(3, ge=1)
    pivot_min_age: int = Field(6, ge=1)
    pivot_max_age: int = Field(72, ge=2)
    oi_change_threshold_pct: float = Field(0.25, ge=0)
    funding_supportive: float = Field(0.0001, ge=0)
    funding_extreme: float = Field(0.0003, ge=0)
    volume_multiplier: float = Field(1.30, ge=1)
    signal_min_score: float = Field(4.5, ge=0)
    strong_signal_score: float = Field(6.0, ge=0)
    signal_cooldown_min: int = Field(30, ge=0)

    ticker_persist_seconds: float = Field(5, gt=0)
    heartbeat_seconds: float = Field(20, gt=0)
    ws_stale_seconds: float = Field(30, gt=0)
    ws_alert_seconds: float = Field(60, gt=0)
    tracking_fee_bps_per_side: float = Field(0, ge=0)
    tracking_slippage_bps_per_side: float = Field(0, ge=0)

    # Research collection is opt-in and does not change V1's Bybit inputs.
    archive_enabled: bool = False
    archive_venues: tuple[str, ...] = ("bybit", "binance", "okx", "coinbase")
    archive_grace_seconds: int = Field(10, ge=1, le=60)
    archive_poll_seconds: int = Field(60, ge=30)
    archive_queue_size: int = Field(5000, ge=100, le=50000)
    binance_spot_rest_url: str = "https://api.binance.com"
    binance_linear_rest_url: str = "https://fapi.binance.com"
    binance_spot_ws_url: str = "wss://stream.binance.com:9443/ws"
    binance_linear_ws_url: str = "wss://fstream.binance.com/market/ws"
    okx_rest_url: str = "https://openapi.okx.com"
    okx_public_ws_url: str = "wss://ws.okx.com:8443/ws/v5/public"
    coinbase_rest_url: str = "https://api.exchange.coinbase.com"
    coinbase_ws_url: str = "wss://ws-feed.exchange.coinbase.com"

    coinalyze_enabled: bool = False
    coinalyze_api_key: SecretStr | None = Field(None, repr=False, exclude=True)
    coinalyze_poll_seconds: int = Field(120, ge=60)
    coinalyze_requests_per_minute: int = Field(25, ge=1, le=30)
    # Spot units require reference checks; the official funding schema uses percent.
    coinalyze_spot_volume_unit: str = "unknown"
    coinalyze_funding_unit: str = "percent"

    # Independent 1H research alerts; never changes V1 settings.
    hourly_enabled: bool = False
    hourly_telegram_enabled: bool = False
    hourly_source: str = "coinalyze"
    # Production default replaces legacy pressure alerts; rollback remains explicit.
    hourly_strategy: str = "sweep"
    hourly_decision_delay_seconds: int = Field(180, ge=30, le=600)
    hourly_context_hours: int = Field(48, ge=30, le=72)
    hourly_flow_hours: int = Field(6, ge=2, le=12)
    hourly_pressure_threshold: float = Field(0.05, gt=0, le=0.5)
    hourly_price_move_atr: float = Field(0.15, ge=0, le=2)
    hourly_level_tolerance_atr: float = Field(0.25, gt=0, le=1)
    hourly_cooldown_hours: int = Field(4, ge=1, le=24)
    dashboard_password: SecretStr | None = Field(None, repr=False, exclude=True)

    @field_validator("hourly_source")
    @classmethod
    def validate_hourly_source(cls, value):
        if value not in {"coinalyze", "direct"}:
            raise ValueError("HOURLY_SOURCE must be coinalyze/direct")
        return value

    @field_validator("hourly_strategy")
    @classmethod
    def validate_hourly_strategy(cls, value):
        if value not in {"sweep", "pressure", "combined"}:
            raise ValueError("HOURLY_STRATEGY must be sweep/pressure/combined")
        return value

    @field_validator("coinalyze_api_key", mode="before")
    @classmethod
    def normalize_coinalyze_key(cls, value):
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("coinalyze_spot_volume_unit")
    @classmethod
    def validate_spot_volume_unit(cls, value):
        if value not in {"unknown", "base", "quote"}:
            raise ValueError("Coinalyze spot volume unit must be unknown/base/quote")
        return value

    @field_validator("coinalyze_funding_unit")
    @classmethod
    def validate_funding_unit(cls, value):
        if value not in {"unknown", "fraction", "percent"}:
            raise ValueError("Coinalyze funding unit must be unknown/fraction/percent")
        return value

    @field_validator("database_url")
    @classmethod
    def normalize_database_url(cls, value: str) -> str:
        if value.startswith("postgres://"):
            return value.replace("postgres://", "postgresql+asyncpg://", 1)
        if value.startswith("postgresql://") and "+asyncpg" not in value:
            return value.replace("postgresql://", "postgresql+asyncpg://", 1)
        return value

    @field_validator("telegram_bot_token", "telegram_chat_id")
    @classmethod
    def empty_to_none(cls, value: str | None) -> str | None:
        return value or None

    def validate_runtime(self) -> None:
        if self.telegram_enabled and not (self.telegram_bot_token and self.telegram_chat_id):
            raise ValueError(
                "Telegram is enabled but TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID is missing"
            )
        if self.pivot_max_age <= self.pivot_min_age:
            raise ValueError("PIVOT_MAX_AGE must be greater than PIVOT_MIN_AGE")
        if not self.archive_venues or len(set(self.archive_venues)) != len(self.archive_venues):
            raise ValueError("ARCHIVE_VENUES must be a non-empty unique list")
        if set(self.archive_venues) - {"bybit", "binance", "okx", "coinbase"}:
            raise ValueError("Unsupported ARCHIVE_VENUES entry")
        if self.coinalyze_enabled and not self.coinalyze_api_key:
            raise ValueError("COINALYZE_ENABLED requires COINALYZE_API_KEY")
        if self.coinalyze_enabled and not self.archive_enabled:
            raise ValueError("COINALYZE_ENABLED requires ARCHIVE_ENABLED")
        if self.hourly_telegram_enabled and not (self.hourly_enabled and self.telegram_enabled):
            raise ValueError("HOURLY_TELEGRAM_ENABLED requires HOURLY_ENABLED and TELEGRAM_ENABLED")


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.validate_runtime()
    return settings
