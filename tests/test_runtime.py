from pathlib import Path

from btc_monitor.config import PROJECT_ROOT, Settings
from btc_monitor.runtime import MonitorRuntime


def test_warmup_requires_twelve_five_minute_bars_per_market():
    runtime = MonitorRuntime.__new__(MonitorRuntime)
    runtime.settings = Settings(cvd_warmup_bars=12)
    assert runtime._warmup_complete({"spot": 59, "linear": 60}) is False
    assert runtime._warmup_complete({"spot": 60, "linear": 60}) is True


def test_env_file_is_resolved_from_project_root():
    assert Path(Settings.model_config["env_file"]) == PROJECT_ROOT / ".env"
