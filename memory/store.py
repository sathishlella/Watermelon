"""Two-layer persistent memory: Capability Memory + Execution Memory.

This is the part the brief cares about most, so a few design notes:

* Memory is **structured knowledge**, not a log. We do not store raw prompts and
  retrieve "similar" ones. We store (a) reusable plans keyed by a canonical
  intent signature, (b) constraints discovered at runtime (validation rules,
  rate limits, permission boundaries), and (c) per-capability success rates.
  The planner and executor *read* these to make different decisions.

* It is plain JSON on disk so the "before/after" state is trivially inspectable
  on the walkthrough call (`main.py --show-memory`). It survives across
  sessions and is never wiped automatically.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from config import MEMORY_DIR


def normalize(text: str) -> str:
    """Cheap canonicalisation used for exact-match plan reuse (0 LLM calls)."""
    text = text.lower().strip()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[.!?;:]+$", "", text)
    return text


def _now_ms() -> float:
    return time.time() * 1000.0


# --------------------------------------------------------------------------- #
# Capability Memory                                                           #
# --------------------------------------------------------------------------- #
class CapabilityMemory:
    """What the agent knows how to do, how well it works, and what limits apply."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.capabilities: dict[str, dict] = {}
        self._load()
        if not self.capabilities:
            self._seed_builtins()
            self.save()

    # ---- persistence ----
    def _load(self) -> None:
        if os.path.exists(self.path):
            with open(self.path) as f:
                self.capabilities = json.load(f)

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(self.capabilities, f, indent=2)

    # ---- reads ----
    def has(self, name: str) -> bool:
        return name in self.capabilities

    def get(self, name: str) -> dict | None:
        return self.capabilities.get(name)

    def names(self) -> list[str]:
        return sorted(self.capabilities)

    def success_rate(self, name: str) -> float | None:
        cap = self.capabilities.get(name)
        if not cap or cap["stats"]["uses"] == 0:
            return None
        return cap["stats"]["successes"] / cap["stats"]["uses"]

    def catalog(self) -> list[dict]:
        """Compact view the planner sees: name, description, kind, reliability."""
        out = []
        for cap in self.capabilities.values():
            out.append(
                {
                    "name": cap["name"],
                    "kind": cap["kind"],
                    "description": cap["description"],
                    "success_rate": self.success_rate(cap["name"]),
                    "constraints": cap.get("constraints", []),
                }
            )
        return out

    # ---- writes ----
    def register(self, cap: dict) -> None:
        cap.setdefault("origin", "synthesized")
        cap.setdefault("constraints", [])
        cap.setdefault(
            "stats", {"uses": 0, "successes": 0, "failures": 0, "total_latency_ms": 0.0}
        )
        self.capabilities[cap["name"]] = cap
        self.save()

    def record_use(self, name: str, success: bool, latency_ms: float) -> None:
        cap = self.capabilities.get(name)
        if not cap:
            return
        s = cap["stats"]
        s["uses"] += 1
        s["successes" if success else "failures"] += 1
        s["total_latency_ms"] += latency_ms
        self.save()

    def add_constraint(self, name: str, text: str) -> None:
        cap = self.capabilities.get(name)
        if not cap:
            return
        if text not in cap["constraints"]:
            cap["constraints"].append(text)
            self.save()

    # ---- seed ----
    def _seed_builtins(self) -> None:
        """A deliberately MINIMAL set. Everything else is synthesised at runtime.

        We hand-write only: list issues, create issue (the two operations needed
        to bootstrap), plus pure in-process data transforms. Labelling,
        commenting, assigning, creating files, etc. do NOT exist here — the agent
        has to build them the first time it needs them.
        """
        builtins = [
            {
                "name": "list_issues",
                "kind": "api",
                "origin": "builtin",
                "description": (
                    "List repository issues. Args: state(open|closed|all), "
                    "assignee('none'=unassigned, '*'=any, or a login), "
                    "labels(comma-separated), per_page, page, sort, direction."
                ),
                "descriptor": {
                    "method": "GET",
                    "path": "/repos/{owner}/{repo}/issues",
                    "query": ["state", "assignee", "labels", "per_page", "page",
                              "sort", "direction"],
                    "body": {},
                    "extract": None,
                },
            },
            {
                "name": "create_issue",
                "kind": "api",
                "origin": "builtin",
                "description": (
                    "Create an issue. Args: title(required), body, "
                    "labels(list), assignees(list)."
                ),
                "descriptor": {
                    "method": "POST",
                    "path": "/repos/{owner}/{repo}/issues",
                    "query": [],
                    "body": {"title": "title", "body": "body",
                             "labels": "labels", "assignees": "assignees"},
                    "extract": None,
                },
            },
            # ---- pure transforms (no API cost) ----
            {"name": "filter", "kind": "transform", "origin": "builtin",
             "description": "Filter a list. Args: items, where{field,op,value}. "
                            "ops: eq,ne,in,contains,exists,empty,not_empty."},
            {"name": "group_by", "kind": "transform", "origin": "builtin",
             "description": "Group a list into {key:[items]}. Args: items, field. "
                            "List-valued fields (e.g. labels[].name) place an item "
                            "under each value."},
            {"name": "count_by", "kind": "transform", "origin": "builtin",
             "description": "Count a list into {key:count}. Args: items, field."},
            {"name": "extract_field", "kind": "transform", "origin": "builtin",
             "description": "Pull one field from each item. Args: items, field."},
            {"name": "format_template", "kind": "transform", "origin": "builtin",
             "description": "Render a markdown string. Args: template, data. "
                            "Use {placeholders} filled from data."},
        ]
        for cap in builtins:
            self.register(cap)


# --------------------------------------------------------------------------- #
# Execution Memory                                                            #
# --------------------------------------------------------------------------- #
class ExecutionMemory:
    """What the agent has done before: decompositions, outcomes, metrics, notes."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.records: list[dict] = []
        self._load()

    def _load(self) -> None:
        if os.path.exists(self.path):
            with open(self.path) as f:
                self.records = json.load(f)

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(self.records, f, indent=2)

    def add(self, record: dict) -> None:
        self.records.append(record)
        self.save()

    # ---- reads used by the planner ----
    def find_reusable_exact(self, norm: str) -> dict | None:
        """Most recent *successful* run of a byte-for-byte similar instruction."""
        for rec in reversed(self.records):
            if rec["norm"] == norm and rec["outcome"] in ("success", "partial"):
                return rec
        return None

    def find_reusable_similar(self, signature: str) -> dict | None:
        """Most recent successful run with the same canonical intent signature."""
        for rec in reversed(self.records):
            if rec["signature"] == signature and rec["outcome"] in ("success", "partial"):
                return rec
        return None

    def notes_for(self, signature: str) -> list[str]:
        """Union of every optimisation hint learned for this pattern."""
        notes: list[str] = []
        for rec in self.records:
            if rec["signature"] == signature:
                for n in rec.get("learned_notes", []):
                    if n not in notes:
                        notes.append(n)
        return notes

    def run_index(self, signature: str) -> int:
        """How many times this pattern has run before (so we can say 'run N')."""
        return sum(1 for rec in self.records if rec["signature"] == signature)

    def history(self, signature: str) -> list[dict]:
        runs = [rec for rec in self.records if rec["signature"] == signature]
        return [
            {
                "run": i + 1,
                "outcome": r["outcome"],
                "plan_source": r.get("executed_source", r["plan"].get("source")),
                "metrics": r["metrics"],
                "timestamp": r["timestamp"],
            }
            for i, r in enumerate(runs)
        ]


# --------------------------------------------------------------------------- #
# Facade                                                                       #
# --------------------------------------------------------------------------- #
class Memory:
    """Bundles both layers and exposes inspectable snapshots."""

    def __init__(self, directory: str = MEMORY_DIR) -> None:
        self.directory = directory
        self.capabilities = CapabilityMemory(os.path.join(directory, "capabilities.json"))
        self.executions = ExecutionMemory(os.path.join(directory, "executions.json"))

    def snapshot(self) -> dict:
        """A compact, comparable view for before/after reporting."""
        caps = self.capabilities.capabilities
        return {
            "capability_count": len(caps),
            "synthesized_capabilities": sorted(
                c["name"] for c in caps.values() if c.get("origin") == "synthesized"
            ),
            "total_constraints": sum(len(c.get("constraints", [])) for c in caps.values()),
            "execution_records": len(self.executions.records),
        }
