import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / 'app' / 'data'
STATIC_DIR = ROOT / 'static'
ForecastEngine = Literal['auto', 'stat', 'timesfm']


def forecast_engine() -> ForecastEngine:
    value = os.getenv('FORECAST_ENGINE', 'auto').strip().lower()
    if value not in ('auto', 'stat', 'timesfm'):
        raise ValueError('FORECAST_ENGINE must be auto, stat or timesfm')
    return value


@dataclass(frozen=True)
class Settings:
    db_path: Path = field(default_factory=lambda: Path(os.getenv('DB_PATH', ROOT / 'data' / 'decision_engine.sqlite')))
    port: int = field(default_factory=lambda: int(os.getenv('PORT', '8780')))
    llm_provider: str = field(default_factory=lambda: os.getenv('LLM_PROVIDER', 'codex'))
    openrouter_api_key: str | None = field(default_factory=lambda: os.getenv('OPENROUTER_API_KEY'), repr=False)
    llm_base_url: str = field(default_factory=lambda: os.getenv('LLM_BASE_URL', 'https://openrouter.ai/api/v1'))
    llm_model: str = field(default_factory=lambda: os.getenv('LLM_MODEL', 'openai/gpt-4o-mini'))
    solver_time_limit: float = field(default_factory=lambda: float(os.getenv('SOLVER_TIME_LIMIT', '15')))
    forecast_engine: ForecastEngine = field(default_factory=forecast_engine)
    codex_home: Path = field(default_factory=lambda: Path(os.getenv('CODEX_HOME') or Path.home() / '.codex'))
