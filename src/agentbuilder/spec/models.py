"""Agent spec and eval report models (see design/03-generated-agent.md, design/05-eval-loop.md).

`AgentSpec` mirrors `agent.spec.yaml`, the source of truth for what the target agent should
do. Like scenario files it rejects unknown keys. `EvalReport` is what the evaluators produce
from one iteration's traces; its aggregates are computed from the per-run results.
"""

import math
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from agentbuilder_runtime.scenarios import ID_PATTERN, ScenarioSet


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _unique(kind: str, names: list[str]) -> None:
    seen: set[str] = set()
    for name in names:
        if name in seen:
            raise ValueError(f"duplicate {kind}: {name!r}")
        seen.add(name)


# --- Agent spec ---------------------------------------------------------------------------


class OutputSpec(_Strict):
    kind: Literal["structured", "text"]
    schema_ref: str | None = Field(default=None, pattern=r"^[\w.]+:\w+$")
    """`module:Class` of the Output model; required for `structured`, absent for `text`."""

    @model_validator(mode="after")
    def _ref_matches_kind(self) -> Self:
        if self.kind == "structured" and self.schema_ref is None:
            raise ValueError("structured output needs schema_ref")
        if self.kind == "text" and self.schema_ref is not None:
            raise ValueError("text output takes no schema_ref")
        return self


class DepSpec(_Strict):
    """A runtime input passed to `build_deps` (scenario `deps`, later the run form)."""

    name: str = Field(pattern=r"^[a-z_][a-z0-9_]*$")
    type: Literal["str", "int", "float", "bool", "path"]
    description: str = ""


class ToolSpec(_Strict):
    name: str = Field(pattern=r"^[a-z_][a-z0-9_]*$")
    purpose: str
    backed_by: str | None = None
    """Domain library function or module the tool adapts, e.g. `schema_kb.search`."""


class DomainLibrary(_Strict):
    package: str = Field(pattern=r"^[a-z_][a-z0-9_]*$")
    rationale: str
    modules: list[str] = []


class Permissions(_Strict):
    network: Literal["none", "allowlist"] = "none"
    """Network for the agent's own tools; the model proxy is always reachable."""
    allowlist: list[str] = []

    @model_validator(mode="after")
    def _allowlist_matches_mode(self) -> Self:
        if self.network == "none" and self.allowlist:
            raise ValueError("allowlist given but network is 'none'")
        return self


class HardRequirement(_Strict):
    """Must hold on every case that lists it; one violation blocks Done."""

    id: str = Field(pattern=ID_PATTERN)
    description: str


class Acceptance(_Strict):
    min_pass_rate: float = Field(default=0.9, ge=0, le=1)
    hard_requirements: list[HardRequirement] = []
    max_cost_per_run_usd: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        _unique("hard requirement", [r.id for r in self.hard_requirements])
        return self


class ChangelogEntry(_Strict):
    version: int = Field(ge=1)
    summary: str


class AgentSpec(_Strict):
    version: int = Field(default=1, ge=1)
    """Bumped by every approved spec change."""
    name: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    purpose: str
    model: str
    """Pydantic AI model string the agent is built and probed with."""
    instructions_file: str
    """System prompt, relative to the workspace root."""
    output: OutputSpec
    deps: list[DepSpec] = []
    tools: list[ToolSpec] = []
    domain_library: DomainLibrary | None = None
    permissions: Permissions = Permissions()
    acceptance: Acceptance = Acceptance()
    changelog: list[ChangelogEntry] = []

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        _unique("dep", [d.name for d in self.deps])
        _unique("tool", [t.name for t in self.tools])
        for entry in self.changelog:
            if entry.version > self.version:
                raise ValueError(f"changelog entry v{entry.version} is newer than the spec")
        return self

    def check_scenarios(self, scenarios: ScenarioSet) -> list[str]:
        """Problems that make the scenario set inconsistent with this spec (empty if none)."""
        hard = {r.id for r in self.acceptance.hard_requirements}
        deps = {d.name for d in self.deps}
        tools = {t.name for t in self.tools}
        problems: list[str] = []
        for case in scenarios.cases:
            problems += [
                f"{case.id}: unknown hard requirement {h!r}"
                for h in case.expect.hard
                if h not in hard
            ]
            problems += [f"{case.id}: undeclared dep {d!r}" for d in case.deps if d not in deps]
            expect = case.expect
            named = [
                *expect.tools_called_include,
                *expect.tools_called_exclude,
                *expect.tools_called_in_order,
            ]
            problems += [
                f"{case.id}: unknown tool {t!r}" for t in dict.fromkeys(named) if t not in tools
            ]
        return problems

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


# --- Eval report --------------------------------------------------------------------------


class CheckResult(BaseModel):
    """One expectation evaluated against one run."""

    name: str
    """Expectation key (`tools_called_include`, `output_matches.total`, `judge`, ...)."""
    passed: bool
    reason: str = ""
    requirement: str | None = None
    """Hard requirement id this check enforces, if any."""
    score: float | None = Field(default=None, ge=0, le=1)
    """LLM-judge score; None for deterministic checks."""


class RunResult(BaseModel):
    """Evaluation of one probe (one repeat of a case)."""

    probe_id: str
    checks: list[CheckResult] = []
    error: str | None = None
    """The run itself failed (exception, timeout); its checks are then not meaningful."""
    cost_usd: float | None = None
    duration_s: float = 0

    @computed_field
    @property
    def passed(self) -> bool:
        return self.error is None and all(c.passed for c in self.checks)


type CaseChange = Literal["new", "fixed", "regressed", "still_passing", "still_failing"]


class CaseResult(BaseModel):
    scenario_id: str
    tags: list[str] = []
    runs: list[RunResult] = Field(min_length=1)
    change: CaseChange | None = None
    """Compared with the previous iteration's report; None when there is none."""

    @computed_field
    @property
    def pass_rate(self) -> float:
        return sum(r.passed for r in self.runs) / len(self.runs)

    @computed_field
    @property
    def passed(self) -> bool:
        """Strict: every repeat passed."""
        return all(r.passed for r in self.runs)

    @computed_field
    @property
    def flaky(self) -> bool:
        return 0 < self.pass_rate < 1

    @computed_field
    @property
    def hard_violations(self) -> list[str]:
        return sorted(
            {
                c.requirement
                for r in self.runs
                for c in r.checks
                if c.requirement is not None and not c.passed
            }
        )


def _percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile; None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(math.ceil(q * len(ordered)) - 1, 0)]


class EvalReport(BaseModel):
    iteration: int = Field(ge=0)
    spec_version: int = Field(ge=1)
    cases: list[CaseResult] = []

    @model_validator(mode="after")
    def _unique_cases(self) -> Self:
        _unique("case", [c.scenario_id for c in self.cases])
        return self

    @computed_field
    @property
    def pass_rate(self) -> float:
        return sum(c.passed for c in self.cases) / len(self.cases) if self.cases else 0.0

    @computed_field
    @property
    def pass_rate_by_tag(self) -> dict[str, float]:
        by_tag: dict[str, list[bool]] = {}
        for case in self.cases:
            for tag in case.tags:
                by_tag.setdefault(tag, []).append(case.passed)
        return {tag: sum(v) / len(v) for tag, v in sorted(by_tag.items())}

    @computed_field
    @property
    def hard_violations(self) -> list[str]:
        return sorted({h for c in self.cases for h in c.hard_violations})

    @computed_field
    @property
    def regressed(self) -> list[str]:
        return [c.scenario_id for c in self.cases if c.change == "regressed"]

    @computed_field
    @property
    def cost_usd(self) -> float:
        """Sum over runs with a known cost."""
        return sum(r.cost_usd for c in self.cases for r in c.runs if r.cost_usd is not None)

    @computed_field
    @property
    def latency_p50_s(self) -> float | None:
        return _percentile([r.duration_s for c in self.cases for r in c.runs], 0.5)

    @computed_field
    @property
    def latency_p95_s(self) -> float | None:
        return _percentile([r.duration_s for c in self.cases for r in c.runs], 0.95)

    def compared_to(self, previous: EvalReport | None) -> EvalReport:
        """Copy with each case's `change` set relative to `previous`."""
        if previous is None:
            return self
        before = {c.scenario_id: c.passed for c in previous.cases}
        cases: list[CaseResult] = []
        for case in self.cases:
            was = before.get(case.scenario_id)
            change: CaseChange
            if was is None:
                change = "new"
            elif was:
                change = "still_passing" if case.passed else "regressed"
            else:
                change = "fixed" if case.passed else "still_failing"
            cases.append(case.model_copy(update={"change": change}))
        return self.model_copy(update={"cases": cases})

    def meets(self, acceptance: Acceptance, *, allow_regressions: bool = False) -> bool:
        """The Done criterion: pass rate reached, no hard violation, no regression."""
        return (
            self.pass_rate >= acceptance.min_pass_rate
            and not self.hard_violations
            and (allow_regressions or not self.regressed)
        )

    @classmethod
    def load(cls, path: Path) -> Self:
        return cls.model_validate_json(path.read_bytes())

    def save(self, path: Path) -> None:
        path.write_text(self.model_dump_json(indent=2), encoding="utf-8")
