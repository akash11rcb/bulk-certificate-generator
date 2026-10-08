"""Application settings, read from environment variables with sensible defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw else default


@dataclass(frozen=True)
class Settings:
    # Any SQLAlchemy URL works, e.g. postgresql+psycopg://user:pass@host/db
    database_url: str = field(
        default_factory=lambda: os.getenv("DATABASE_URL", "sqlite:///./data/certificates.db")
    )
    # Where generated PDFs are written. One sub-folder per job.
    storage_dir: Path = field(
        default_factory=lambda: Path(os.getenv("STORAGE_DIR", "./data/certificates"))
    )
    # Upper bound on recipients in a single request (protects memory / DB).
    max_recipients_per_job: int = field(
        default_factory=lambda: _env_int("MAX_RECIPIENTS_PER_JOB", 5000)
    )
    # Number of jobs processed concurrently by the in-process worker pool.
    worker_threads: int = field(default_factory=lambda: _env_int("WORKER_THREADS", 2))
    # Public URL printed on each certificate so anyone can verify it.
    public_base_url: str = field(
        default_factory=lambda: os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
    )


def get_settings() -> Settings:
    return Settings()
