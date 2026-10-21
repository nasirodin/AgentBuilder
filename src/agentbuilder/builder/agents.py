"""Builder agents. Only the main (front desk) agent exists so far."""

from pathlib import Path

from google.oauth2 import service_account
from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.google_cloud import GoogleCloudProvider

from agentbuilder.config import Settings, get_settings

PROMPTS_DIR = Path(__file__).parent / "prompts"


def resolve_model(settings: Settings) -> Model | str:
    """Build the model named by `settings.model`, wiring in keys from settings."""
    name = settings.model
    if name == "test":
        return TestModel()
    provider, _, model_name = name.partition(":")
    if provider == "anthropic":
        key = settings.anthropic_api_key
        return AnthropicModel(
            model_name,
            provider=AnthropicProvider(api_key=key.get_secret_value() if key else None),
        )
    if provider == "google":
        key = settings.google_api_key
        return GoogleModel(
            model_name,
            provider=GoogleProvider(api_key=key.get_secret_value() if key else None),
        )
    if provider in ("google-cloud", "google-vertex"):  # google-vertex: Pydantic AI 1.x name
        return GoogleModel(model_name, provider=_google_cloud_provider(settings))
    return name  # other providers: let Pydantic AI resolve the string


def _google_cloud_provider(settings: Settings) -> GoogleCloudProvider:
    """Vertex AI: service-account file or ADC with a project; else API key (Express Mode)."""
    credentials = None
    project = settings.google_cloud_project
    if settings.google_application_credentials:
        credentials = service_account.Credentials.from_service_account_file(
            settings.google_application_credentials
        )
        project = project or credentials.project_id
    key = settings.google_api_key
    use_key = key is not None and credentials is None and project is None
    return GoogleCloudProvider(
        api_key=key.get_secret_value() if use_key else None,
        credentials=credentials,
        project=project,
        location=settings.google_cloud_location,
    )


def build_main_agent(
    model: Model | str | None = None, settings: Settings | None = None
) -> Agent[None, str]:
    if model is None:
        model = resolve_model(settings or get_settings())
    return Agent(
        model,
        instructions=(PROMPTS_DIR / "main.md").read_text(encoding="utf-8"),
        name="main",
    )
