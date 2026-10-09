from btc_monitor.config import Settings


def test_railway_postgres_url_is_normalized():
    settings = Settings(database_url="postgresql://user:pass@host/db")
    assert settings.database_url.startswith("postgresql+asyncpg://")
