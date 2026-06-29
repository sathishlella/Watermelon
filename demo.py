"""Scripted walkthrough: three instructions of increasing complexity, each run
twice so the before/after numbers are visible in one shot.

    python demo.py            # run all three, twice each
    python demo.py --once     # run each once (first-encounter behaviour only)

Memory is NOT wiped between or within demo runs — that is the point.
"""
from __future__ import annotations

import sys

from rich.console import Console

import config
from agent.core import Agent
from main import render_report, show_stats

console = Console()

# Increasing complexity: builtin only -> synthesis -> compound decomposition.
INSTRUCTIONS = [
    "Create an issue titled 'Login times out after 30s' describing that users "
    "are logged out mid-session, and label it 'bug'.",

    "Find every open issue that has no assignee and add the label "
    "'needs-triage' to each of them.",

    "Find all open issues, group them by label, and create a single summary "
    "issue titled 'Weekly Triage Summary' listing how many open issues fall "
    "under each label.",
]


def main(argv: list[str]) -> int:
    config.require_env()
    once = "--once" in argv
    rounds = 1 if once else 2
    agent = Agent()  # one agent, shared persistent memory

    for i, instruction in enumerate(INSTRUCTIONS, 1):
        console.rule(f"[bold magenta]Instruction {i}")
        for r in range(rounds):
            console.print(f"\n[bold]— run {r + 1} —[/]")
            report = agent.run(instruction)
            render_report(report)
        if rounds > 1:
            console.print()
            show_stats(instruction)

    console.rule("[bold]Final memory state")
    console.print("Run [bold]python main.py --show-memory[/] to inspect it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
