"""The agent: plan -> execute -> reflect -> remember.

The learning loop lives here. After a run that did real work (a fresh plan, a
synthesis, or a recovery), the agent REFLECTS: it asks itself what it now knows
that would make the next run of this pattern cheaper or more reliable, and folds
that back into (a) reusable hints and (b) an improved plan stored under the
pattern's signature. The next identical instruction therefore replays a better
plan with zero planning/synthesis LLM calls.
"""
from __future__ import annotations

import json
import time
import uuid

from agent.executor import Executor
from agent.models import ExecutionReport, Plan, Step
from agent.planner import Planner
from agent.synthesis import Synthesizer
from github_client import GitHubClient
from llm import LLM
from memory.store import Memory, normalize


class Agent:
    def __init__(self) -> None:
        self.llm = LLM()
        self.github = GitHubClient()
        self.memory = Memory()
        self.synth = Synthesizer(self.llm, self.github, self.memory.capabilities)
        self.planner = Planner(self.llm, self.memory.executions, self.memory.capabilities)
        self.executor = Executor(self.llm, self.github, self.memory, self.synth)

    # ------------------------------------------------------------------ #
    def run(self, instruction: str) -> ExecutionReport:
        mem_before = self.memory.snapshot()
        llm0, api0 = self.llm.snapshot(), self.github.snapshot()
        t0 = time.time()

        plan = self.planner.plan(instruction)
        results, status, decisions, effects = self.executor.execute(plan)

        metrics = {
            "api_calls": self.github.snapshot() - api0,
            "llm_calls": self.llm.snapshot() - llm0,
            "duration_ms": round((time.time() - t0) * 1000),
            "steps": len(plan.steps),
            "plan_source": plan.source,
            "run_index": self.memory.executions.run_index(plan.signature) + 1,
        }

        # ---- reflect & learn (only when there's something new to learn) ----
        reuse_plan, notes = plan, []
        if status in ("success", "partial") and (
            plan.source != "memory:exact" or decisions
        ):
            notes, improved = self._reflect(instruction, plan, results, decisions, metrics)
            if improved is not None:
                reuse_plan = improved

        record_id = uuid.uuid4().hex[:8]
        self.memory.executions.add({
            "id": record_id,
            "instruction": instruction,
            "norm": normalize(instruction),
            "signature": plan.signature,
            "plan": reuse_plan.to_dict(),       # plan to REPLAY next time (improved)
            "executed_source": plan.source,     # provenance of THIS run
            "outcome": status,
            "metrics": metrics,
            "learned_notes": notes,
            "effects": effects,                 # mutations made (for rollback)
            "rolled_back": False,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })

        mem_after = self.memory.snapshot()
        return ExecutionReport(
            instruction=instruction, status=status, plan=plan, results=results,
            metrics=metrics, decisions=decisions,
            memory_before=mem_before, memory_after=mem_after,
            effects=effects, record_id=record_id,
        )

    # ------------------------------------------------------------------ #
    def rollback(self, execution_id: str | None = None,
                 reason: str = "instructed") -> ExecutionReport:
        """Undo the mutations of a past run by synthesising/executing inverse ops.

        Finds the target run (most recent with un-rolled-back effects, or by id),
        asks the LLM to reason out the compensating actions in reverse order, then
        executes them through the same executor+synthesis path (so inverse ops
        like remove-label / delete-label / close-issue are built on demand).
        """
        rec = self._find_rollbackable(execution_id)
        if rec is None:
            raise SystemExit("Nothing to roll back (no run with recorded effects).")

        mem_before = self.memory.snapshot()
        llm0, api0 = self.llm.snapshot(), self.github.snapshot()
        t0 = time.time()

        inverse = self._inverse_plan(rec)
        results, status, decisions, _ = self.executor.execute(inverse)

        rec["rolled_back"] = True
        rec["rollback"] = {"reason": reason, "outcome": status,
                           "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S")}
        self.memory.executions.save()

        metrics = {
            "api_calls": self.github.snapshot() - api0,
            "llm_calls": self.llm.snapshot() - llm0,
            "duration_ms": round((time.time() - t0) * 1000),
            "steps": len(inverse.steps),
            "plan_source": "rollback",
            "run_index": 0,
        }
        return ExecutionReport(
            instruction=f"ROLLBACK of «{rec['instruction'][:60]}» ({reason})",
            status=status, plan=inverse, results=results, metrics=metrics,
            decisions=decisions, memory_before=mem_before,
            memory_after=self.memory.snapshot(), record_id=rec["id"],
        )

    def _find_rollbackable(self, execution_id):
        recs = self.memory.executions.records
        if execution_id:
            return next((r for r in recs if r["id"] == execution_id), None)
        for r in reversed(recs):
            if r.get("effects") and not r.get("rolled_back"):
                return r
        return None

    def _inverse_plan(self, rec) -> Plan:
        system = (
            "You roll back a GitHub agent's run. Given the mutations it made (in "
            "order), output the compensating actions that undo them, in REVERSE "
            "order. Conventions: a created issue cannot be deleted via REST — "
            "undo it by closing it (set state=closed). Undo an applied label by "
            "removing it from the issue. Undo a created label by deleting it. "
            "Undo a created comment by deleting it.\n"
            "Reuse capabilities from the catalog when present; otherwise invent a "
            "snake_case name, set expect_synthesis=true, and describe the exact "
            "GitHub REST call (method+path). Pull concrete ids/numbers/names from "
            "each mutation's result.\n"
            'Respond ONLY as JSON: {"steps":[{"id","capability","args",'
            '"description","expect_synthesis"}]}'
        )
        user = json.dumps({
            "mutations_made": rec.get("effects", []),
            "capability_catalog": self.memory.capabilities.catalog(),
        }, indent=2, default=str)
        out = self.llm.complete_json(system, user, max_tokens=2500)
        steps = [Step.from_dict({**s, "id": s.get("id", f"r{i+1}")})
                 for i, s in enumerate(out.get("steps", []))]
        return Plan(signature=f"rollback:{rec['signature']}", steps=steps,
                    source="rollback")

    # ------------------------------------------------------------------ #
    def _reflect(self, instruction, plan: Plan, results, decisions, metrics):
        """Extract reusable knowledge + (optionally) a cheaper plan for next time."""
        used_caps = {}
        for r in results:
            cap = self.memory.capabilities.get(r.step.capability)
            if cap:
                used_caps[cap["name"]] = cap.get("constraints", [])

        system = (
            "You improve an autonomous GitHub agent after a run. Using the plan "
            "that ran, the runtime decisions, and the constraints discovered, "
            "produce:\n"
            "1) notes: 0-3 concise, reusable hints for THIS instruction pattern "
            "(imperative, specific, e.g. 'ensure the label exists before "
            "applying it', 'filter server-side with assignee=none&state=open').\n"
            "2) improved_steps: an improved step list that makes the NEXT "
            "identical run cheaper or more reliable — fold a discovered "
            "prerequisite into the plan, switch list+filter into a server-side "
            "filtered query, or drop a redundant call. Reuse existing capability "
            "names from the catalog (synthesised ones now exist and are free to "
            "reuse). Only return improved_steps if you are confident it is "
            "correct; otherwise null.\n\n"
            "Respond ONLY as JSON: {\"notes\":[...], \"improved_steps\": "
            "[{\"id\",\"capability\",\"args\",\"description\",\"expect_synthesis\"}] "
            "| null}"
        )
        user = json.dumps({
            "instruction": instruction,
            "executed_plan": plan.to_dict(),
            "runtime_decisions": decisions,
            "capabilities_used_and_constraints": used_caps,
            "capability_catalog": self.memory.capabilities.catalog(),
            "metrics": metrics,
        }, indent=2, default=str)

        try:
            out = self.llm.complete_json(system, user, max_tokens=2500)
        except Exception:
            return [], None

        notes = [n for n in out.get("notes", []) if isinstance(n, str)][:3]
        improved = None
        raw_steps = out.get("improved_steps")
        if isinstance(raw_steps, list) and raw_steps and all(
            isinstance(s, dict) and s.get("capability") for s in raw_steps
        ):
            try:
                steps = [Step.from_dict({**s, "id": s.get("id", f"s{i+1}")})
                         for i, s in enumerate(raw_steps)]
                improved = Plan(signature=plan.signature, steps=steps,
                                source="memory:improved", notes=notes)
            except Exception:
                improved = None
        return notes, improved
