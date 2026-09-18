"""Runtime configuration.

All settings can be overridden with environment variables prefixed ``SNP500_``
(or a ``.env`` file in the working directory). Paths are relative to the
project root unless absolute.
"""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SNP500_", env_file=".env", extra="ignore")

    # Database. Default is a SQLite file so the pipeline and tests run with zero
    # infrastructure; production uses PostgreSQL via this URL (see docker-compose.yml).
    database_url: str = f"sqlite:///{PROJECT_ROOT / 'data' / 'build' / 'snp500.sqlite3'}"

    raw_dir: Path = PROJECT_ROOT / "data" / "raw"
    reference_dir: Path = PROJECT_ROOT / "data" / "reference"
    curated_dir: Path = PROJECT_ROOT / "data" / "curated"
    build_dir: Path = PROJECT_ROOT / "data" / "build"

    user_agent: str = "snp500-research/0.1 (https://github.com/OjasPhadake/snp500)"
    requests_per_second: float = 1.0
    http_timeout_seconds: float = 60.0
    http_retries: int = 4

    # Primary analysis window (inclusive). Data outside is kept but flagged.
    window_start: str = "2001-01-01"
    window_end: str = "2026-12-31"

    def resolve(self, p: Path) -> Path:
        return p if p.is_absolute() else PROJECT_ROOT / p


settings = Settings()
