"""Scenario set: `evals/scenarios.yaml` in a target workspace (see design/03-generated-agent.md).

Lives in the runtime package because the probe runner reads it inside the target venv.
Scenario files are hand-written or LLM-written, so unknown keys are rejected: a misspelled
expectation would otherwise silently check nothing.
"""

import re
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

ID_PATTERN = r"^[a-z0-9][a-z0-9_-]*$"
"""Scenario ids end up in probe ids and file names."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Expectations(_Strict):
    """What a run of the case must satisfy. Everything except `judge` is checked in code.

    Output paths are dotted keys into the JSON form of the agent's output (`total`,
    `items.0.name`); the empty path `""` is the whole output (use it for text agents).
    """

    tools_called_include: list[str] = []
    """Each of these tools is called at least once."""
    tools_called_exclude: list[str] = []
    """None of these tools is called."""
    tools_called_in_order: list[str] = []
    """These calls appear in this order (other calls may come between them)."""
    max_tool_calls: int | None = Field(default=None, ge=0)
    output_matches: dict[str, JsonValue] = {}
    """Output path -> expected value (exact JSON equality)."""
    output_regex: dict[str, str] = {}
    """Output path -> regex that must match somewhere in the value's string form."""
    hard: list[str] = []
    """Ids of the spec's hard requirements that must hold for this case."""
    max_cost_usd: float | None = Field(default=None, ge=0)
    max_duration_s: float | None = Field(default=None, gt=0)
    judge: str | None = None
    """Rubric for the LLM judge; the only non-deterministic expectation."""

    @field_validator("output_regex")
    @classmethod
    def _compiles(cls, v: dict[str, str]) -> dict[str, str]:
        for path, pattern in v.items():
            try:
                re.compile(pattern)
            except re.error as e:
                raise ValueError(f"output_regex[{path!r}]: {e}") from None
        return v


class Scenario(_Strict):
    id: str = Field(pattern=ID_PATTERN)
    input: str
    """The task given to the agent (the user prompt)."""
    deps: dict[str, JsonValue] = {}
    """Values passed to `build_deps`; relative paths are relative to `evals/`."""
    expect: Expectations = Expectations()
    tags: list[str] = []


class ScenarioSet(_Strict):
    cases: list[Scenario]

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        seen: set[str] = set()
        for case in self.cases:
            if case.id in seen:
                raise ValueError(f"duplicate scenario id: {case.id!r}")
            seen.add(case.id)
        return self

    def get(self, scenario_id: str) -> Scenario:
        for case in self.cases:
            if case.id == scenario_id:
                return case
        raise KeyError(scenario_id)

    @classmethod
    def from_yaml(cls, text: str) -> Self:
        return cls.model_validate(yaml.safe_load(text))

    def to_yaml(self) -> str:
        data = self.model_dump(mode="json", exclude_defaults=True)
        return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)

    @classmethod
    def load(cls, path: Path) -> Self:
        return cls.from_yaml(path.read_text(encoding="utf-8"))

    def save(self, path: Path) -> None:
        path.write_text(self.to_yaml(), encoding="utf-8")
