# AgentBuilder

A terminal app where you describe the agent you need and a builder agent designs, implements,
runs, probes and fixes it — entirely inside a sandbox — using Pydantic AI.

**Status:** design phase. Start with [design/README.md](design/README.md).

## Run the chat TUI

```sh
cp .env.example .env          # non-secret settings (model, ...)
cp .secret.example .secret    # API keys — fill in ANTHROPIC_API_KEY
uv run agentbuilder
```

- Settings live in `src/agentbuilder/config.py` (pydantic-settings). Priority: environment
  variables > `.secret` > `.env` > defaults. Both files are git-ignored and read from the
  current directory.
- `AGENTBUILDER_MODEL` is any Pydantic AI model string (default `anthropic:claude-opus-5-5`);
  e.g. `google:gemini-3.8-flash` (needs `GOOGLE_API_KEY`); `test` runs offline with canned replies.
- Vertex AI: `AGENTBUILDER_MODEL=google-vertex:gemini-3.8-flash` (or `google-cloud:`) plus
  `GOOGLE_CLOUD_PROJECT` / `GOOGLE_CLOUD_LOCATION` in `.env`. Auth is Application Default
  Credentials (`gcloud auth application-default login`) or a service-account file via
  `GOOGLE_APPLICATION_CREDENTIALS` in `.secret`.
- `Ctrl+L` clears the screen, `Ctrl+Q` quits.
- Tests: `uv run pytest`.

## Lint

```sh
uv run ruff check .            # lint (add --fix to auto-fix)
uv run ruff format .           # format (use --check in CI)
```

The same checks run in GitHub Actions on every push and pull request
(`.github/workflows/lint.yml`). Rules and settings are in `[tool.ruff]` in `pyproject.toml`.
