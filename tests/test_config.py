import pytest
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.test import TestModel

from agentbuilder.builder.agents import resolve_model
from agentbuilder.config import Settings


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENTBUILDER_MODEL", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return tmp_path


def test_defaults_without_files() -> None:
    s = Settings()
    assert s.model == "anthropic:claude-opus-5-5"
    assert s.anthropic_api_key is None
    assert s.google_cloud_location == "global"


def test_reads_env_and_secret_files(isolated) -> None:
    (isolated / ".env").write_text("AGENTBUILDER_MODEL=anthropic:claude-sonnet-5-5\n")
    (isolated / ".secret").write_text("ANTHROPIC_API_KEY=sk-test\n")
    s = Settings()
    assert s.model == "anthropic:claude-sonnet-5-5"
    assert s.anthropic_api_key.get_secret_value() == "sk-test"
    assert "sk-test" not in repr(s)


def test_environment_overrides_files(isolated, monkeypatch) -> None:
    (isolated / ".env").write_text("AGENTBUILDER_MODEL=anthropic:claude-sonnet-5-5\n")
    monkeypatch.setenv("AGENTBUILDER_MODEL", "test")
    assert Settings().model == "test"


def test_resolve_model_uses_settings_key() -> None:
    model = resolve_model(Settings(model="anthropic:claude-opus-5-5", ANTHROPIC_API_KEY="sk-test"))
    assert isinstance(model, AnthropicModel)
    assert model.model_name == "claude-opus-5-5"
    assert isinstance(resolve_model(Settings(model="test")), TestModel)


@pytest.mark.parametrize("prefix", ["google-vertex", "google-cloud"])
def test_resolve_vertex_model(isolated, prefix) -> None:
    (isolated / ".env").write_text(
        f"AGENTBUILDER_MODEL={prefix}:gemini-3.8-flash\n"
        "GOOGLE_CLOUD_PROJECT=my-proj\nGOOGLE_CLOUD_LOCATION=global\n"
    )
    settings = Settings()
    assert (settings.google_cloud_project, settings.google_cloud_location) == ("my-proj", "global")
    model = resolve_model(settings)  # ADC is resolved lazily, so no credentials needed here
    assert isinstance(model, GoogleModel)
    assert model.model_name == "gemini-3.8-flash"


def test_resolve_google_model(isolated) -> None:
    (isolated / ".secret").write_text("GEMINI_API_KEY=g-test\n")
    settings = Settings(model="google:gemini-3.8-flash")
    assert settings.google_api_key.get_secret_value() == "g-test"
    model = resolve_model(settings)
    assert isinstance(model, GoogleModel)
    assert model.model_name == "gemini-3.8-flash"
