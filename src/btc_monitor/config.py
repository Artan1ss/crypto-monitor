from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
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


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.validate_runtime()
    return settings
