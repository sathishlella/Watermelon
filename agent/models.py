"""Plain data structures passed between planner, executor, and core."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Step:
    """One unit of work in a plan."""
    id: str                       # e.g. "s1"
    capability: str               # capability name to invoke
    args: dict[str, Any] = field(default_factory=dict)
    description: str = ""
    # If true, this capability is expected NOT to exist yet and must be
    # synthesised at runtime before it can run.
    expect_synthesis: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "capability": self.capability,
            "args": self.args,
            "description": self.description,
            "expect_synthesis": self.expect_synthesis,
        }

    @staticmethod
    def from_dict(d: dict) -> "Step":
        return Step(
            id=d["id"],
            capability=d["capability"],
            args=d.get("args", {}),
            description=d.get("description", ""),
            expect_synthesis=d.get("expect_synthesis", False),
        )


@dataclass
class Plan:
    signature: str                # canonical intent signature
    steps: list[Step]
    source: str                   # "memory:exact" | "memory:similar" | "llm"
    notes: list[str] = field(default_factory=list)  # learned hints applied

    def to_dict(self) -> dict:
        return {
            "signature": self.signature,
            "steps": [s.to_dict() for s in self.steps],
            "source": self.source,
            "notes": self.notes,
        }

    @staticmethod
    def from_dict(d: dict) -> "Plan":
        return Plan(
            signature=d["signature"],
            steps=[Step.from_dict(s) for s in d["steps"]],
            source=d.get("source", "memory:exact"),
            notes=d.get("notes", []),
        )


def summarize_output(out: Any, limit: int = 12) -> str:
    """A short, human-readable view of a step's output (count + key fields)."""
    if out is None or out == "":
        return ""
    if isinstance(out, list):
        n = len(out)
        if n == 0:
            return "0 items"
        head = "; ".join(_one_line(it) for it in out[:limit])
        more = f" …(+{n - limit} more)" if n > limit else ""
        return f"{n} item(s): {head}{more}"
    if isinstance(out, dict):
        return _one_line(out)
    return str(out)[:300]


def _one_line(it: Any) -> str:
    if not isinstance(it, dict):
        return str(it)
    num, title = it.get("number"), it.get("title") or it.get("name")
    parts = []
    if num is not None:
        parts.append(f"#{num}")
    if title:
        parts.append(str(title))
    if it.get("state"):
        parts.append(f"[{it['state']}]")
    if not parts:
        parts.append(it.get("html_url") or ", ".join(
            f"{k}={v}" for k, v in list(it.items())[:3]))
    return " ".join(parts)


@dataclass
class StepResult:
    step: Step
    status: str                   # ok | failed | skipped | synthesized
    output: Any = None
    error: str | None = None
    api_calls: int = 0
    decision: str | None = None   # what the agent decided to do about a problem

    def to_dict(self) -> dict:
        out = self.output
        preview = summarize_output(out)
        # Keep the persisted report compact: summarise large list outputs.
        if isinstance(out, list) and len(out) > 5:
            out = f"<{len(out)} items>"
        return {
            "step": self.step.id,
            "capability": self.step.capability,
            "status": self.status,
            "output": out,
            "output_preview": preview,
            "error": self.error,
            "api_calls": self.api_calls,
            "decision": self.decision,
        }


@dataclass
class ExecutionReport:
    instruction: str
    status: str                   # success | partial | failed
    plan: Plan
    results: list[StepResult]
    metrics: dict[str, Any]
    decisions: list[str]
    memory_before: dict
    memory_after: dict
    effects: list = field(default_factory=list)   # mutations made (for rollback)
    record_id: str | None = None                  # execution-memory id of this run

    def result_summary(self) -> str:
        """Human-readable output of the last step that produced data."""
        for r in reversed(self.results):
            if r.status in ("ok", "synthesized") and r.output not in (None, "", []):
                return summarize_output(r.output, limit=25)
        return ""

    def to_dict(self) -> dict:
        return {
            "instruction": self.instruction,
            "status": self.status,
            "plan_source": self.plan.source,
            "plan_notes": self.plan.notes,
            "steps": [r.to_dict() for r in self.results],
            "result_summary": self.result_summary(),
            "metrics": self.metrics,
            "decisions": self.decisions,
            "memory_before": self.memory_before,
            "memory_after": self.memory_after,
            "effects": self.effects,
            "record_id": self.record_id,
        }
