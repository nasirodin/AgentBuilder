from pathlib import Path

import pytest
from pydantic import ValidationError

from agentbuilder.spec import (
    Acceptance,
    AgentSpec,
    CaseResult,
    CheckResult,
    EvalReport,
    RunResult,
    ScenarioSet,
)

EXAMPLES = Path(__file__).parent.parent / "examples"

SPEC_YAML = """\
version: 3
name: schema-sql-agent
purpose: Propose safe read-only SQL for analysts from a DDL dump.
model: anthropic:claude-sonnet-5-5
instructions_file: src/schema_sql_agent/prompts/system.md
output:
  kind: structured
  schema_ref: schema_sql_agent.agent:Output
deps:
- name: ddl_path
  type: path
  description: Path to DDL dump
tools:
- name: search_tables
  purpose: Fuzzy search tables/columns by keyword
  backed_by: schema_kb.search
- name: describe_table
  purpose: Return columns, types, keys for one table
  backed_by: schema_kb.catalog
domain_library:
  package: schema_kb
  rationale: DDL parsing and catalog lookups are deterministic and testable.
  modules:
  - parser
  - catalog
  - search
  - sql_guard
acceptance:
  hard_requirements:
  - id: read_only
    description: Proposed SQL never contains non-SELECT statements.
  max_cost_per_run_usd: 0.05
changelog:
- version: 3
  summary: Require describe_table before referencing a column.
"""


def minimal(**overrides: object) -> dict[str, object]:
    return {
        "name": "a",
        "purpose": "p",
        "model": "test",
        "instructions_file": "src/a/prompts/system.md",
        "output": {"kind": "text"},
        **overrides,
    }


def test_spec_yaml_round_trip(tmp_path: Path) -> None:
    spec = AgentSpec.from_yaml(SPEC_YAML)
    assert spec.version == 3
    assert spec.acceptance.min_pass_rate == 0.9  # default
    assert spec.permissions.network == "none"
    assert spec.to_yaml() == SPEC_YAML
    spec.save(tmp_path / "agent.spec.yaml")
    assert AgentSpec.load(tmp_path / "agent.spec.yaml") == spec


@pytest.mark.parametrize(
    "overrides",
    [
        {"output": {"kind": "structured"}},
        {"output": {"kind": "text", "schema_ref": "a.agent:Output"}},
        {"permissions": {"network": "none", "allowlist": ["example.com"]}},
        {"tools": [{"name": "t", "purpose": "x"}, {"name": "t", "purpose": "y"}]},
        {"changelog": [{"version": 2, "summary": "from the future"}]},
        {"acceptance": {"min_pass_rate": 1.5}},
        {"name": "Bad Name"},
        {"modle": "typo"},
    ],
    ids=[
        "structured-without-ref",
        "text-with-ref",
        "allowlist-without-network",
        "duplicate-tool",
        "changelog-ahead",
        "pass-rate-range",
        "bad-name",
        "unknown-key",
    ],
)
def test_invalid_spec(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(minimal(**overrides))


def test_check_scenarios_reports_unknown_references() -> None:
    spec = AgentSpec.from_yaml(SPEC_YAML)
    scenarios = ScenarioSet.model_validate(
        {
            "cases": [
                {
                    "id": "ok",
                    "input": "x",
                    "deps": {"ddl_path": "fixtures/shop.sql"},
                    "expect": {"hard": ["read_only"], "tools_called_include": ["describe_table"]},
                },
                {
                    "id": "bad",
                    "input": "x",
                    "deps": {"db_url": "x"},
                    "expect": {
                        "hard": ["no_pii"],
                        "tools_called_include": ["drop_table"],
                        "tools_called_in_order": ["drop_table"],
                    },
                },
            ]
        }
    )
    assert spec.check_scenarios(scenarios) == [
        "bad: unknown hard requirement 'no_pii'",
        "bad: undeclared dep 'db_url'",
        "bad: unknown tool 'drop_table'",
    ]


def test_calculator_example_is_consistent() -> None:
    root = EXAMPLES / "calculator"
    spec = AgentSpec.load(root / "agent.spec.yaml")
    scenarios = ScenarioSet.load(root / "evals" / "scenarios.yaml")
    assert spec.check_scenarios(scenarios) == []
    assert len(scenarios.cases) >= 3


def run(
    probe_id: str,
    *,
    passed: bool = True,
    hard: str | None = None,
    cost_usd: float | None = None,
    duration_s: float = 0,
) -> RunResult:
    check = CheckResult(name="c", passed=passed, requirement=hard)
    return RunResult(probe_id=probe_id, checks=[check], cost_usd=cost_usd, duration_s=duration_s)


def case(scenario_id: str, *runs: RunResult, tags: list[str] | None = None) -> CaseResult:
    return CaseResult(scenario_id=scenario_id, runs=list(runs), tags=tags or [])


def test_report_aggregates() -> None:
    report = EvalReport(
        iteration=2,
        spec_version=1,
        cases=[
            case("a", run("a1", cost_usd=0.01, duration_s=1), tags=["happy"]),
            case("b", run("b1", duration_s=2), run("b2", passed=False, duration_s=4)),
            case("c", run("c1", passed=False, hard="read_only", cost_usd=0.02, duration_s=3)),
            case("d", RunResult(probe_id="d1", error="timeout", duration_s=30), tags=["happy"]),
        ],
    )
    a, b, c, d = report.cases
    assert (a.passed, b.passed, b.pass_rate, b.flaky, c.flaky) == (True, False, 0.5, True, False)
    assert not d.passed
    assert report.pass_rate == 0.25
    assert report.pass_rate_by_tag == {"happy": 0.5}
    assert report.hard_violations == ["read_only"]
    assert report.cost_usd == pytest.approx(0.03)
    assert report.latency_p50_s == 3
    assert report.latency_p95_s == 30
    assert not report.meets(Acceptance(min_pass_rate=0.2))  # hard violation


def test_report_diff_and_done() -> None:
    before = EvalReport(
        iteration=1,
        spec_version=1,
        cases=[case("a", run("a1")), case("b", run("b1", passed=False)), case("c", run("c1"))],
    )
    after = EvalReport(
        iteration=2,
        spec_version=1,
        cases=[
            case("a", run("a2")),
            case("b", run("b2")),
            case("c", run("c2", passed=False)),
            case("d", run("d2")),
        ],
    ).compared_to(before)
    assert [c.change for c in after.cases] == ["still_passing", "fixed", "regressed", "new"]
    assert after.regressed == ["c"]
    assert not after.meets(Acceptance(min_pass_rate=0.5))
    assert after.meets(Acceptance(min_pass_rate=0.5), allow_regressions=True)
    assert not after.meets(Acceptance(min_pass_rate=0.9), allow_regressions=True)


def test_report_json_round_trip(tmp_path: Path) -> None:
    report = EvalReport(iteration=0, spec_version=1, cases=[case("a", run("a1", duration_s=1))])
    report.save(tmp_path / "report.json")
    loaded = EvalReport.load(tmp_path / "report.json")  # computed fields are ignored on load
    assert loaded == report
    assert loaded.pass_rate == 1.0


def test_empty_report() -> None:
    report = EvalReport(iteration=0, spec_version=1)
    assert (report.pass_rate, report.latency_p50_s) == (0.0, None)
    with pytest.raises(ValidationError):
        CaseResult(scenario_id="a", runs=[])
