from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

RunStatus = Literal["needs_input", "completed", "partial", "failed"]


class Action(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=500)
    sop_ids: list[str] = Field(default_factory=list, max_length=3)


class Narrative(BaseModel):
    """LLM writes procedural suggestions, never authoritative business numbers."""

    model_config = ConfigDict(extra="forbid")
    actions: list[Action] = Field(default_factory=list, max_length=5)
    notes: list[str] = Field(default_factory=list, max_length=5)


class Report(BaseModel):
    order_id: str
    as_of: str
    facts: dict[str, Any]
    anomalies: list[dict[str, Any]]
    data_gaps: list[str]
    evidence: list[dict[str, Any]]
    sop_refs: list[dict[str, Any]]
    actions: list[Action]
    followup_draft: str | None = None
    notes: list[str] = Field(default_factory=list)


class Metrics(BaseModel):
    elapsed_ms: float = 0
    tool_calls: int = 0
    model_rounds: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=2000)
    session_id: str | None = Field(default=None, max_length=64)


class ChatResponse(BaseModel):
    session_id: str
    run_id: str
    status: RunStatus
    answer: str
    report: Report | None = None
    markdown: str = ""
    metrics: Metrics
    mode: Literal["demo", "cloud"]
    session_reset: bool = False

