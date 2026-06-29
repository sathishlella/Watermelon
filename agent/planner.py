"""Turns a natural-language instruction into an executable Plan.

The planner is where the *learning shows up first*. Before doing any LLM work it
asks execution memory whether it has solved this (or a similar) instruction
before. If so it reuses the stored decomposition — which means a repeated
instruction costs **zero planning LLM calls** and carries forward every
optimisation hint learned on previous runs.
"""
from __future__ import annotations

import json

from agent.models import Plan, Step
from llm import LLM
from memory.store import ExecutionMemory, normalize


class Planner:
    def __init__(self, llm: LLM, executions: ExecutionMemory, capabilities) -> None:
        self.llm = llm
        self.executions = executions
        self.capabilities = capabilities

    # ------------------------------------------------------------------ #
    def plan(self, instruction: str) -> Plan:
        norm = normalize(instruction)

        # 1) Exact reuse: same instruction succeeded before -> 0 LLM calls.
        rec = self.executions.find_reusable_exact(norm)
        if rec:
            plan = Plan.from_dict(rec["plan"])
            plan.source = "memory:exact"
            plan.notes = self.executions.notes_for(plan.signature)
            return plan

        # 2) Compute the canonical intent signature (1 LLM call).
        signature = self._signature(instruction)

        # 3) Similar reuse: a different wording of the same intent succeeded.
        rec = self.executions.find_reusable_similar(signature)
        if rec:
            plan = self._rebind(instruction, rec, signature)
            plan.source = "memory:similar"
            plan.notes = self.executions.notes_for(signature)
            return plan

        # 4) Fresh decomposition, informed by the capability catalog + any hints
        #    carried over from related patterns.
        plan = self._decompose(instruction, signature)
        plan.source = "llm"
        return plan

    # ------------------------------------------------------------------ #
    def _signature(self, instruction: str) -> str:
        system = (
            "You convert a GitHub automation instruction into a short canonical "
            "intent signature: snake_case, value-independent, capturing the "
            "operation and object only. Examples:\n"
            "'Create a bug report for the login timeout' -> create_issue\n"
            "'Label every unassigned open issue needs-triage' -> "
            "label_unassigned_open_issues\n"
            "'Group open issues by label and post a summary issue' -> "
            "summarize_open_issues_by_label\n"
            'Respond ONLY as JSON: {"signature": "..."}'
        )
        out = self.llm.complete_json(system, f"Instruction: {instruction}")
        return out["signature"].strip()

    def _decompose(self, instruction: str, signature: str) -> Plan:
        catalog = self.capabilities.catalog()
        notes = self.executions.notes_for(signature)
        system = (
            "You are the planner for an autonomous GitHub agent. Decompose the "
            "instruction into an ordered list of executable steps.\n\n"
            "RULES:\n"
            "- Prefer capabilities that already exist in the catalog.\n"
            "- If an operation is needed that is NOT in the catalog, invent a "
            "snake_case capability name, set \"expect_synthesis\": true, and write "
            "a PRECISE description of the GitHub REST operation it must perform "
            "(endpoint, method, what it returns). The agent will build it.\n"
            "- Reference an earlier step's output with \"$s1\" (whole output) or "
            "\"$s1.field\". For per-item fan-out over a list, set "
            "\"for_each\": \"$s1\" in args; each item's fields are then "
            "available as \"$item.field\" AND auto-exposed by name. ALWAYS pass "
            "the item identifier the operation needs, e.g. "
            '"issue_number": "$item.number".\n'
            "- Array-valued GitHub fields MUST be JSON arrays, e.g. "
            '"labels": ["needs-triage"] (never a bare string).\n'
            "- Keep plans minimal; do not add verification steps unless required.\n"
            "- Apply the learned hints if present.\n\n"
            "Example fan-out step (label each issue in a list):\n"
            '{"id":"s2","capability":"add_label_to_issue","args":'
            '{"for_each":"$s1","issue_number":"$item.number",'
            '"labels":["needs-triage"]},"description":"POST /repos/{owner}/'
            '{repo}/issues/{issue_number}/labels","expect_synthesis":true}\n\n'
            "Respond ONLY as JSON:\n"
            '{"steps":[{"id":"s1","capability":"name","args":{},'
            '"description":"...","expect_synthesis":false}]}'
        )
        user = (
            f"Instruction: {instruction}\n\n"
            f"Capability catalog:\n{json.dumps(catalog, indent=2)}\n\n"
            f"Learned hints for this pattern:\n{json.dumps(notes)}"
        )
        out = self.llm.complete_json(system, user, max_tokens=3000)
        steps = [Step.from_dict(s) for s in out["steps"]]
        return Plan(signature=signature, steps=steps, source="llm", notes=notes)

    def _rebind(self, instruction: str, rec: dict, signature: str) -> Plan:
        """Reuse a prior plan's structure, re-binding only literal arguments."""
        prior = rec["plan"]
        system = (
            "Reuse the prior plan's STRUCTURE exactly (same steps, same "
            "capabilities, same order). Only update literal argument values so "
            "they match the new instruction. Respond ONLY as JSON with the same "
            "shape: {\"steps\":[...]}."
        )
        user = (
            f"New instruction: {instruction}\n\n"
            f"Prior plan:\n{json.dumps(prior, indent=2)}"
        )
        out = self.llm.complete_json(system, user, max_tokens=3000)
        steps = [Step.from_dict(s) for s in out["steps"]]
        return Plan(signature=signature, steps=steps, source="memory:similar",
                    notes=self.executions.notes_for(signature))
