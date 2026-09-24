"""Application settings. Every runtime knob comes from environment variables.

Copy .env.example to .env and adjust. Every variable has a local-dev default so
the API runs out of the box with the static data bundle.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    # -- app ---------------------------------------------------------------
    app_name: str = "BerthSide API"
    app_version: str = "0.1.0"
    environment: str = "development"  # development | production
    cors_origins: str = "*"  # comma-separated list; "*" allows all (dev only)
    public_base_url: str = "http://127.0.0.1:8000"

    # -- data --------------------------------------------------------------
    # Either a local folder containing inbox/ + attachments/ (static bundle),
    # or the HTTP dataset server URL (e.g. http://localhost:8080).
    #
    # A relative path is resolved against BACKEND_ROOT (the repository root),
    # not the current working directory, so `data/corpus` means the same thing
    # whether the app is started by `start_server.bat` (cwd = repo root), by
    # `python -m uvicorn` from the repo root, or from anywhere else. `.env` is
    # per-machine and git-ignored, so a relative path there is the portable
    # choice; an absolute path still works unchanged.
    data_source: str = "data/corpus"

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

    # -- Gmail integration -------------------------------------------------
    # Dedicated demo mailbox. This is an address, not a password; OAuth tokens
    # and client secrets stay outside git and are read from the paths below.
    gmail_demo_account: str = "averis.demo@gmail.com"
    gmail_credentials_file: str = "secrets/google_oauth_client.json"
    gmail_token_file: str = "secrets/gmail_token.json"
    gmail_oauth_redirect_uri: str = "http://127.0.0.1:8000/api/gmail/oauth-callback"
    gmail_query: str = "in:inbox"
    gmail_max_results: int = 10
    gmail_polling_enabled: bool = False
    gmail_poll_interval_seconds: int = 60
    gmail_client_id: str = ""
    gmail_client_secret: str = ""
    gmail_refresh_token: str = ""

    @field_validator("gmail_credentials_file", "gmail_token_file")
    @classmethod
    def _anchor_secret_file(cls, value: str) -> str:
        """Anchor relative credential/token file paths to BACKEND_ROOT."""
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = BACKEND_ROOT / path
        return str(path.resolve())

    # -- database (Dual-Track Physical Isolation) ---------------------------
    # 1. Enterprise Hub DB: 520 dataset, carrier EDI, gateway quarantine, audits
    database_url_enterprise: str = f"sqlite:///{BACKEND_ROOT / 'data' / 'sdoc_enterprise.db'}"
    # 2. OAuth DB: personal operator real Gmail mailbox, operator tokens & verdicts
    database_url_oauth: str = f"sqlite:///{BACKEND_ROOT / 'data' / 'sdoc_oauth.db'}"
    # Default / legacy database URL (aliased to enterprise hub)
    database_url: str = f"sqlite:///{BACKEND_ROOT / 'data' / 'sdoc_enterprise.db'}"

    # -- AI ----------------------------------------------------------------
    # "rule"     -> deterministic built-in classifier/extractor (default, offline)
    # "remote"   -> forward to P3's AI microservice over HTTP
    # "hybrid"   -> remote first, fall back to rule engine on failure
    # "cascade"  -> multi-provider LLM gateway: Gemini > Zhipu > Qwen > Rule fallback
    # "ollama"   -> local Ollama VLM (qwen2.5vl:7b) only; no cloud key needed
    ai_provider: str = "rule"
    ai_service_url: str = ""  # e.g. http://localhost:8001
    ai_api_url: str = ""      # alias for ai_service_url / ollama_base_url
    ai_api_key: str = ""
    ai_timeout_seconds: float = 30.0
    ai_max_retries: int = 2

    # Cloud LLM Provider API Keys
    dashscope_api_key: str = ""
    gemini_api_key: str = ""
    zhipuai_api_key: str = ""

    # Ollama endpoint: local install, LAN box, or a public HTTPS endpoint
    # (tunnel / reverse proxy / cloud GPU). Only the URL changes.
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5vl:7b"
    ollama_api_key: str = ""  # sent as Bearer; only needed behind an auth proxy

    # Liveness probe budget for /api/tags. Kept short so a warm endpoint answers
    # instantly; a timeout is retried once on the cold-start budget below, so a
    # host that is waking up is not reported as offline. Raise the cold-start
    # value when the endpoint sits behind a tunnel or a sleeping cloud GPU.
    ollama_probe_timeout_seconds: float = 2.0
    ollama_cold_start_timeout_seconds: float = 90.0
    # /api/chat budget. Loading the model into VRAM happens here, not in the
    # probe, so this is the knob that actually absorbs a cold model.
    ollama_request_timeout_seconds: float = 180.0

    @model_validator(mode="after")
    def _sync_ai_settings(self) -> Settings:
        """Sync AI_API_URL and AI_API_KEY into ollama_base_url and ai_service_url."""
        if self.ai_api_url:
            if not self.ai_service_url:
                self.ai_service_url = self.ai_api_url
            if self.ollama_base_url in ("http://localhost:11434", ""):
                self.ollama_base_url = self.ai_api_url
        if self.ai_api_key:
            if not self.ollama_api_key:
                self.ollama_api_key = self.ai_api_key
        return self

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

    # -- email channel (real IMAP inbound & SMTP / Resend outbound) --------
    # Resend.com REST API (HTTPS:443 - works seamlessly on Render.com)
    resend_api_key: str = ""
    resend_from: str = "Averis BerthSide Hub <onboarding@resend.dev>"

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_use_tls: bool = True
    smtp_use_ssl: bool = False

    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    imap_folder: str = "INBOX"
    imap_poll_interval_seconds: int = 10
    imap_enabled: bool = False
    auto_reply_on_verification: bool = True
    extra_trusted_sources: str = ""  # comma-separated extra trusted emails, e.g. you@gmail.com

    model_config = SettingsConfigDict(
        env_file=(
            str(BACKEND_ROOT.parent / ".env"),
            str(BACKEND_ROOT / ".env"),
            ".env",
        ),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
