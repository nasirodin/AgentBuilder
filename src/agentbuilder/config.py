"""Application settings.

Sources, highest priority first: environment variables, `.secret` (API keys and other
credentials), `.env` (non-secret options), field defaults. Both files are read from the
current working directory and are optional.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENTBUILDER_",
        env_file=(".env", ".secret"),  # later files win
        env_file_encoding="utf-8",
        extra="ignore",
    )

    model: str = "anthropic:claude-opus-5-5"
    """Pydantic AI model string; `test` runs offline with canned replies."""

    # Vertex AI (`google-cloud:` / `google-vertex:` models). Standard Google names, no prefix.
    google_cloud_project: str | None = Field(default=None, validation_alias="GOOGLE_CLOUD_PROJECT")
    google_cloud_location: str = Field(default="global", validation_alias="GOOGLE_CLOUD_LOCATION")
    """`global` serves the whole Gemini catalogue; single regions (Pydantic AI's default,
    `us-central1`) and the `us`/`eu` multi-regions serve only a subset."""
    google_application_credentials: Path | None = Field(
        default=None, validation_alias="GOOGLE_APPLICATION_CREDENTIALS"
    )
    """Service-account JSON key file; unset means Application Default Credentials."""

    # Secrets keep their provider-standard names (no prefix).
    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    google_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("GOOGLE_API_KEY", "GEMINI_API_KEY")
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
