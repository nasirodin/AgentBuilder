# AgentBuilder

A terminal app where you describe the agent you need and a builder agent designs, implements,
runs, probes and fixes it — entirely inside a sandbox — using Pydantic AI.

It's for developers who want a working, tested agent from a description instead of hand-iterating
on prompts and tools. Built with Python 3.14, [uv](https://docs.astral.sh/uv/),
[Pydantic AI](https://ai.pydantic.dev), [Textual](https://textual.textualize.io) and
bubblewrap.

**Status:** early development. The design is in [design/README.md](design/README.md).

Working today:

- Chat TUI talking to the builder agent (Anthropic, Gemini or Vertex AI models).
- Sandbox package: bubblewrap + seccomp isolation with domain-filtered network proxies, exposed
  to the builder as a `run_bash` tool (Linux/WSL2).
- Lint and type-check CI.

## TODO

Full plan in [design/09-roadmap.md](design/09-roadmap.md). Next up:

- [ ] Spec models (`AgentSpec`, `Scenario`, `EvalReport`) with YAML round-trip tests.
- [ ] Symlink-safe workspace file tools and host-side git checkpoints.
- [ ] `agentbuilder doctor` prerequisite check and sandbox escape self-test.
- [ ] Walking skeleton: implement → static checks → run scenarios → fix loop, headless.
- [ ] Agent registry: versioned releases, list/search/run from the CLI.
- [ ] TUI Spec/Files/Runs tabs and approval prompts.
- [ ] macOS sandbox backend.

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

## Sandbox

The builder's `run_bash` tool runs commands in `packages/sandbox`
(`agentbuilder_sandbox`). It is a Python re-implementation of the model of Anthropic's
[sandbox-runtime](https://github.com/anthropics/sandbox-runtime): bubblewrap namespaces, a
seccomp filter and host-side filtering proxies (see [design/04-sandbox.md](design/04-sandbox.md)).

- **Platforms:** Linux or WSL2 only, with `bwrap` ≥ 0.5 (`sudo apt-get install bubblewrap`).
  On native Windows the tools report that the sandbox is unavailable and run nothing. To use
  them, run AgentBuilder inside WSL.
- **Sessions:** each run gets a session in `~/.agentbuilder/sessions/<id>/`. Its `workspace/`
  is the agent's working tree.
- **Network:** none by default. Allow domains in `.env`, e.g.
  `AGENTBUILDER_SANDBOX_ALLOWED_DOMAINS=["pypi.org","files.pythonhosted.org"]`.
- **Manual testing:** `uv run abx-sandbox -- sh -c 'cat ~/.ssh/id_rsa; curl -sI https://example.com'`
- **Escape tests:** `packages/sandbox/tests/test_integration.py` is skipped where bubblewrap is
  missing. Run `uv run pytest` inside WSL to exercise it.

## Lint

```sh
uv run ruff check .            # lint (add --fix to auto-fix)
uv run ruff format .           # format (use --check in CI)
uv run pyright                 # type check
```

The same checks run in GitHub Actions on every push and pull request
(`.github/workflows/lint.yml`, `.github/workflows/typecheck.yml`). Settings are in
`[tool.ruff]` and `[tool.pyright]` in `pyproject.toml`.
