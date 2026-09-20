"""Application settings — all runtime knobs come from environment variables.

Copy .env.example to .env and adjust. Every variable has a local-dev default so
the API runs out of the box with the static data bundle.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    # -- app ---------------------------------------------------------------
    app_name: str = "SDOC Shipping Document Verification API"
    app_version: str = "0.1.0"
    environment: str = "development"  # development | production
    cors_origins: str = "*"  # comma-separated list; "*" allows all (dev only)

    # -- data --------------------------------------------------------------
    # Either a local folder containing inbox/ + attachments/ (static bundle),
    # or the HTTP dataset server URL (e.g. http://localhost:8080).
    #
    # A relative path is resolved against BACKEND_ROOT, not the current working
    # directory, so `../sdoc-hackathon-bundle` means the same thing whether the
    # app is started by `start_backend.bat` (cwd = backend/), by
    # `python -m uvicorn` from the repo root, or from anywhere else. `.env` is
    # per-machine and git-ignored, so a relative path there is the portable
    # choice; an absolute path still works unchanged.
    data_source: str = str(Path.home() / "Downloads" / "sdoc-hackathon-bundle")

    @field_validator("data_source")
    @classmethod
    def _anchor_data_source(cls, value: str) -> str:
        """Anchor relative bundle paths to BACKEND_ROOT; leave the rest alone."""
        if value.startswith(("http://", "https://")):  # dataset server
            return value
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = BACKEND_ROOT / path
        return str(path.resolve())

    # Where POST /api/v1/ingest writes the attachments it accepts. Relative
    # paths are anchored to BACKEND_ROOT by the same rule as `data_source`, so
    # the value means the same thing whatever the process working directory is.
    # Tests point this at a temporary directory to stay off the real disk.
    ingest_dir: str = "data/ingested"

    @field_validator("ingest_dir")
    @classmethod
    def _anchor_ingest_dir(cls, value: str) -> str:
        """Anchor a relative ingest directory to BACKEND_ROOT."""
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = BACKEND_ROOT / path
        return str(path.resolve())

    # -- database ----------------------------------------------------------
    # Local dev default: SQLite file next to the backend root.
    # Production (Supabase / any Postgres):
    #   postgresql+psycopg2://postgres:<password>@<host>:5432/postgres
    database_url: str = f"sqlite:///{BACKEND_ROOT / 'sdoc.db'}"

    # -- AI ----------------------------------------------------------------
    # "rule"     -> deterministic built-in classifier/extractor (default, offline)
    # "remote"   -> forward to P3's AI microservice over HTTP
    # "hybrid"   -> remote first, fall back to rule engine on failure
    ai_provider: str = "rule"
    ai_service_url: str = ""  # e.g. http://localhost:8001
    ai_api_key: str = ""
    ai_timeout_seconds: float = 30.0
    ai_max_retries: int = 2

    # -- processing --------------------------------------------------------
    process_max_emails: int = 0  # 0 = no limit for POST /emails/process-all
    log_level: str = "INFO"

    # -- OCR fallback -------------------------------------------------------
    # When a PDF yields no extractable text (scanned / image-only), the engine
    # can attempt OCR via pdf2image + pytesseract to transcribe it instead of
    # escalating as `unreadable`. Requires poppler + tesseract at runtime; when
    # they are absent OCR silently degrades to the existing escalate path (the
    # static bundle is plain text, so this never affects the local score).
    ocr_enabled: bool = True

    model_config = SettingsConfigDict(
        env_file=str(BACKEND_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
