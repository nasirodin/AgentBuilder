"""Trace: the record of one probe, written by the probe runner (see design/05-eval-loop.md).

Only what Pydantic AI does not record itself is added around the message history: which
probe and scenario this was, the final output, and how the run ended. Traces are produced by
our own runner, so unlike scenario files they ignore unknown keys:
a host reading traces from a newer runner keeps working.
"""

from pathlib import Path
from typing import Self

from pydantic import BaseModel, Field, JsonValue


class Usage(BaseModel):
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    """None when the model's price is unknown."""


class TraceError(BaseModel):
    """An exception that ended the run."""

    type: str
    """Qualified exception class, e.g. `pydantic_ai.exceptions.UsageLimitExceeded`."""
    message: str
    traceback: str | None = None


class Trace(BaseModel):
    probe_id: str
    """Unique within a session, e.g. `it3-revenue_by_month-r1`."""
    scenario_id: str | None
    """None for an ad-hoc probe."""
    input: str
    model: str
    messages: list[JsonValue] = []
    """Pydantic AI message history (`all_messages_json`), the single record of what happened
    (tool calls included). Kept raw so reading a trace does not need pydantic-ai; decode with
    `ModelMessagesTypeAdapter` when needed."""
    output: JsonValue = None
    """The agent's output in JSON form; None if the run failed."""
    error: TraceError | None = None
    usage: Usage = Usage()
    duration_s: float = Field(ge=0)

    @property
    def ok(self) -> bool:
        return self.error is None

    @classmethod
    def load(cls, path: Path) -> Self:
        return cls.model_validate_json(path.read_bytes())

    def save(self, path: Path) -> None:
        path.write_text(self.model_dump_json(indent=2), encoding="utf-8")
