"""CLI entry point.

Usage:
  python main.py "natural language instruction"   # run the agent
  python main.py "<instruction>" --auto-rollback   # undo it if it half-fails
  python main.py --rollback [exec_id]              # undo a past run's mutations
  python main.py --show-memory                     # inspect both memory layers
  python main.py --stats "<instruction>"           # before/after run history
  python main.py --reset-memory                    # explicit wipe (never automatic)
"""
from __future__ import annotations

import json
import shutil
import sys

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

import config
from agent.core import Agent
from agent.models import ExecutionReport, summarize_output
from memory.store import Memory, normalize

console = Console()

_STATUS_COLOR = {"success": "green", "partial": "yellow", "failed": "red",
                 "ok": "green", "synthesized": "cyan", "skipped": "dim",
                 "failed_step": "red"}


def render_report(report: ExecutionReport) -> None:
    color = _STATUS_COLOR.get(report.status, "white")
    console.rule(f"[bold]{report.instruction}")
    console.print(
        f"Status: [bold {color}]{report.status.upper()}[/]   "
        f"plan source: [bold]{report.metrics['plan_source']}[/]   "
        f"run #[bold]{report.metrics['run_index']}[/] of this pattern"
    )

    steps = Table("Step", "Capability", "Status", "API calls", "Detail",
                  show_lines=False, expand=True)
    for r in report.results:
        detail = r.error or r.decision or summarize_output(r.output)
        steps.add_row(
            r.step.id, r.step.capability,
            f"[{_STATUS_COLOR.get(r.status,'white')}]{r.status}[/]",
            str(r.api_calls), detail[:90],
        )
    console.print(steps)

    summary = report.result_summary()
    if summary:
        console.print(Panel(summary, title="Result", border_style="cyan"))

    if report.decisions:
        console.print(Panel("\n".join(f"• {d}" for d in report.decisions),
                            title="Agent decisions", border_style="yellow"))

    m = report.metrics
    console.print(
        f"[bold]Metrics[/]  api_calls=[cyan]{m['api_calls']}[/]  "
        f"llm_calls=[magenta]{m['llm_calls']}[/]  "
        f"duration_ms=[blue]{m['duration_ms']}[/]  steps={m['steps']}"
    )

    _render_memory_diff(report.memory_before, report.memory_after)


def _render_memory_diff(before: dict, after: dict) -> None:
    rows = []
    for key in before:
        b, a = before[key], after[key]
        if b != a:
            rows.append((key, b, a))
    if not rows:
        console.print("[dim]Memory unchanged this run.[/]")
        return
    t = Table("Memory", "Before", "After", title="What the agent learned",
              border_style="green")
    for key, b, a in rows:
        t.add_row(key, json.dumps(b) if not isinstance(b, int) else str(b),
                  json.dumps(a) if not isinstance(a, int) else str(a))
    console.print(t)


def show_memory() -> None:
    mem = Memory()
    console.rule("Capability Memory")
    t = Table("Capability", "Origin", "Kind", "Uses", "Success", "Constraints")
    for cap in mem.capabilities.capabilities.values():
        s = cap["stats"]
        rate = mem.capabilities.success_rate(cap["name"])
        t.add_row(
            cap["name"], cap.get("origin", "?"), cap["kind"],
            str(s["uses"]),
            "—" if rate is None else f"{rate:.0%}",
            str(len(cap.get("constraints", []))),
        )
    console.print(t)

    console.rule("Discovered Constraints")
    for cap in mem.capabilities.capabilities.values():
        for c in cap.get("constraints", []):
            console.print(f"  [yellow]{cap['name']}[/]: {c}")

    console.rule("Execution Memory")
    for rec in mem.executions.records[-12:]:
        m = rec["metrics"]
        console.print(
            f"  [{_STATUS_COLOR.get(rec['outcome'],'white')}]{rec['outcome']}[/] "
            f"[bold]{rec['signature']}[/] "
            f"({rec.get('executed_source','?')}) "
            f"api={m['api_calls']} llm={m['llm_calls']} ms={m['duration_ms']}"
        )
        for n in rec.get("learned_notes", []):
            console.print(f"      [dim]learned:[/] {n}")


def show_stats(instruction: str) -> None:
    mem = Memory()
    # Match by signature if any record shares this normalized instruction.
    norm = normalize(instruction)
    sig = None
    for rec in mem.executions.records:
        if rec["norm"] == norm or rec["signature"] == instruction:
            sig = rec["signature"]
            break
    if sig is None:
        console.print(f"[red]No run history for:[/] {instruction}")
        return

    history = mem.executions.history(sig)
    t = Table("Run", "Source", "Outcome", "API calls", "LLM calls", "Duration ms",
              title=f"Learning curve — pattern '{sig}'")
    for h in history:
        m = h["metrics"]
        t.add_row(str(h["run"]), h["plan_source"], h["outcome"],
                  str(m["api_calls"]), str(m["llm_calls"]), str(m["duration_ms"]))
    console.print(t)

    if len(history) >= 2:
        first, last = history[0]["metrics"], history[-1]["metrics"]
        console.print(
            f"[bold green]Run 1 → run {len(history)}:[/] "
            f"api {first['api_calls']}→{last['api_calls']}, "
            f"llm {first['llm_calls']}→{last['llm_calls']}, "
            f"duration {first['duration_ms']}→{last['duration_ms']}ms"
        )


def reset_memory() -> None:
    shutil.rmtree(config.MEMORY_DIR, ignore_errors=True)
    console.print(f"[red]Wiped {config.MEMORY_DIR}/[/]")


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("--help", "-h", "help"):
        console.print(__doc__)
        return 0 if argv else 1

    if argv[0] == "--show-memory":
        show_memory()
        return 0
    if argv[0] == "--reset-memory":
        reset_memory()
        return 0
    if argv[0] == "--stats":
        show_stats(" ".join(argv[1:]))
        return 0
    if argv[0] == "--rollback":
        config.require_env()
        exec_id = argv[1] if len(argv) > 1 else None
        report = Agent().rollback(execution_id=exec_id, reason="instructed")
        render_report(report)
        return 0 if report.status in ("success", "partial") else 2

    auto_rollback = "--auto-rollback" in argv
    argv = [a for a in argv if a != "--auto-rollback"]
    config.require_env()
    instruction = " ".join(argv)
    agent = Agent()
    report = agent.run(instruction)
    render_report(report)

    # Undo a half-finished run if asked to (brief: rollback "if validation fails").
    if auto_rollback and report.status == "partial" and report.effects:
        console.print("\n[yellow]Partial run — auto-rolling back its effects…[/]")
        render_report(agent.rollback(execution_id=report.record_id,
                                      reason="auto: partial failure"))
    return 0 if report.status in ("success", "partial") else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
