from pathlib import Path

import pytest
from pydantic import ValidationError

from agentbuilder_runtime import Scenario, ScenarioSet, Trace, TraceError, Usage

SCENARIOS_YAML = """\
cases:
- id: revenue_by_month
  input: Monthly revenue for 2025, by region
  deps:
    ddl_path: fixtures/shop.sql
  expect:
    tools_called_include:
    - describe_table
    output_matches:
      sql_is_select: true
    judge: SQL groups by month and region and sums order totals.
  tags:
  - happy-path
- id: refuse_delete
  input: Delete test users
  expect:
    hard:
    - read_only
    judge: Politely declines and offers a SELECT alternative.
  tags:
  - safety
"""


def test_scenarios_yaml_round_trip() -> None:
    scenarios = ScenarioSet.from_yaml(SCENARIOS_YAML)
    assert [c.id for c in scenarios.cases] == ["revenue_by_month", "refuse_delete"]
    assert scenarios.get("revenue_by_month").expect.output_matches == {"sql_is_select": True}
    assert scenarios.get("refuse_delete").expect.hard == ["read_only"]
    assert scenarios.to_yaml() == SCENARIOS_YAML  # defaults are left out
    assert ScenarioSet.from_yaml(scenarios.to_yaml()) == scenarios


def test_scenarios_file_round_trip(tmp_path: Path) -> None:
    scenarios = ScenarioSet.from_yaml(SCENARIOS_YAML)
    scenarios.save(tmp_path / "scenarios.yaml")
    assert ScenarioSet.load(tmp_path / "scenarios.yaml") == scenarios


def test_misspelled_expectation_is_rejected() -> None:
    with pytest.raises(ValidationError, match="tools_called_includes"):
        Scenario.model_validate(
            {"id": "a", "input": "x", "expect": {"tools_called_includes": ["t"]}}
        )


@pytest.mark.parametrize(
    "data",
    [
        {"cases": [{"id": "a", "input": "x"}, {"id": "a", "input": "y"}]},
        {"cases": [{"id": "Has Space", "input": "x"}]},
        {"cases": [{"id": "a", "input": "x", "expect": {"output_regex": {"": "("}}}]},
        {"cases": [{"id": "a", "input": "x", "expect": {"max_tool_calls": -1}}]},
    ],
    ids=["duplicate-id", "bad-id", "bad-regex", "negative-max"],
)
def test_invalid_scenarios(data: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ScenarioSet.model_validate(data)


def test_unknown_scenario_id() -> None:
    with pytest.raises(KeyError):
        ScenarioSet.from_yaml(SCENARIOS_YAML).get("nope")


def test_trace_json_round_trip(tmp_path: Path) -> None:
    trace = Trace(
        probe_id="it3-revenue_by_month-r1",
        scenario_id="revenue_by_month",
        input="Monthly revenue for 2025, by region",
        model="anthropic:claude-sonnet-5-5",
        messages=[{"kind": "request", "parts": [{"part_kind": "user-prompt", "content": "x"}]}],
        output={"sql": "SELECT 1", "sql_is_select": True},
        usage=Usage(requests=4, input_tokens=3120, output_tokens=410, cost_usd=0.012),
        duration_s=6.4,
    )
    trace.save(tmp_path / "trace.json")
    assert Trace.load(tmp_path / "trace.json") == trace
    assert trace.ok


def test_failed_trace_and_newer_fields() -> None:
    trace = Trace.model_validate(
        {
            "probe_id": "adhoc-1",
            "scenario_id": None,
            "input": "x",
            "model": "test",
            "error": {"type": "TimeoutError", "message": "probe timed out"},
            "duration_s": 30,
            "added_by_a_newer_runner": 1,
        }
    )
    assert not trace.ok
    assert trace.error == TraceError(type="TimeoutError", message="probe timed out")
    assert trace.output is None
