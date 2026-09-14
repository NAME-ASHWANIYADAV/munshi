"""Application configuration.

Credentials are always optional. When a vendor key is absent (or ``MUNSHIJI_PROVIDER_MODE=local``)
the provider factory serves the offline implementation instead — see SPEC.md §2.1.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["PROJECT_ROOT", "ProviderMode", "Settings", "get_settings"]

#: Repository root (the directory holding ``backend/``, ``frontend/``, ``data/``).
PROJECT_ROOT = Path(__file__).resolve().parents[2]

ProviderMode = Literal["auto", "live", "local"]


class Settings(BaseSettings):
    """Environment-driven settings, loaded once and cached."""

    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env",),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    # ── Core ────────────────────────────────────────────────────────────────
    env: str = Field(default="dev", validation_alias="MUNSHIJI_ENV")
    log_level: str = Field(default="INFO", validation_alias="MUNSHIJI_LOG_LEVEL")
    db_url: str = Field(default="sqlite:///data/munshiji.db", validation_alias="MUNSHIJI_DB_URL")
    provider_mode: ProviderMode = Field(default="auto", validation_alias="MUNSHIJI_PROVIDER_MODE")

    # ── Sarvam AI (Indic voice + LLM) ───────────────────────────────────────
    sarvam_api_key: str = Field(default="", validation_alias="SARVAM_API_KEY")
    sarvam_base_url: str = Field(
        default="https://api.sarvam.ai", validation_alias="SARVAM_BASE_URL"
    )
    sarvam_stt_model: str = Field(default="saaras:v3", validation_alias="SARVAM_STT_MODEL")
    sarvam_llm_model: str = Field(default="sarvam-105b", validation_alias="SARVAM_LLM_MODEL")
    sarvam_tts_model: str = Field(default="bulbul:v3", validation_alias="SARVAM_TTS_MODEL")
    sarvam_tts_speaker: str = Field(default="anushka", validation_alias="SARVAM_TTS_SPEAKER")

    # ── Cognee (knowledge-graph memory) ─────────────────────────────────────
    cognee_api_key: str = Field(default="", validation_alias="COGNEE_API_KEY")
    cognee_base_url: str = Field(
        default="https://platform.cognee.ai", validation_alias="COGNEE_BASE_URL"
    )

    # ── n8n (action orchestration) ──────────────────────────────────────────
    n8n_base_url: str = Field(default="http://localhost:5678", validation_alias="N8N_BASE_URL")
    n8n_webhook_token: str = Field(default="", validation_alias="N8N_WEBHOOK_TOKEN")
    n8n_webhook_prefix: str = Field(default="/webhook", validation_alias="N8N_WEBHOOK_PREFIX")

    # ── Product defaults ────────────────────────────────────────────────────
    default_language: str = Field(default="hi-IN", validation_alias="MUNSHIJI_DEFAULT_LANGUAGE")
    seed: int = Field(default=20260919, validation_alias="MUNSHIJI_SEED")
    max_outbound_per_day: int = Field(default=50, validation_alias="MUNSHIJI_MAX_OUTBOUND_PER_DAY")
    reminder_cooldown_days: int = Field(
        default=7, validation_alias="MUNSHIJI_REMINDER_COOLDOWN_DAYS"
    )
    http_timeout_seconds: float = Field(default=20.0, validation_alias="MUNSHIJI_HTTP_TIMEOUT")

    @field_validator("provider_mode", mode="before")
    @classmethod
    def _normalise_mode(cls, value: object) -> object:
        if isinstance(value, str):
            cleaned = value.strip().lower()
            return cleaned or "auto"
        return value

    # ── Derived paths ───────────────────────────────────────────────────────
    @property
    def project_root(self) -> Path:
        return PROJECT_ROOT

    @property
    def data_dir(self) -> Path:
        path = PROJECT_ROOT / "data"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def audio_dir(self) -> Path:
        path = self.data_dir / "audio"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def sqlalchemy_url(self) -> str:
        """DB URL with relative SQLite paths resolved against the project root."""
        prefix = "sqlite:///"
        if not self.db_url.startswith(prefix):
            return self.db_url
        raw = self.db_url[len(prefix) :]
        path = Path(raw)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        path.parent.mkdir(parents=True, exist_ok=True)
        return f"{prefix}{path.as_posix()}"

    # ── Capability probes (presence of credentials, not liveness) ───────────
    @property
    def has_sarvam(self) -> bool:
        return bool(self.sarvam_api_key.strip())

    @property
    def has_cognee(self) -> bool:
        return bool(self.cognee_api_key.strip())

    @property
    def has_n8n(self) -> bool:
        return bool(self.n8n_webhook_token.strip()) and bool(self.n8n_base_url.strip())

    def wants_live(self, credential_present: bool) -> bool:
        """Whether the factory should attempt a live provider for a given credential state."""
        if self.provider_mode == "local":
            return False
        if self.provider_mode == "live":
            return True
        return credential_present


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings singleton."""
    return Settings()
