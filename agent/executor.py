"""Runs a Plan, step by step, against the real API.

Responsibilities:
* wire step outputs into later steps ($s1, $s1.field, fan-out with $item)
* invoke transforms in-process and API capabilities via their descriptor
* synthesise a capability the moment a step needs one that doesn't exist, and
  TEST it by real execution — refining the descriptor on structural errors and
  recovering from missing-prerequisite errors (e.g. create a label, then retry)
* turn every API failure into a durable constraint in capability memory
* never hide a partial result — once a step hard-fails, downstream steps are
  explicitly skipped and the report says exactly what failed and why
"""
from __future__ import annotations

import json
import re
import time

from agent.descriptor import run_descriptor
from agent.models import Plan, Step, StepResult
from agent.synthesis import Synthesizer
from agent.transforms import run_transform
from config import MAX_SYNTHESIS_ATTEMPTS
from github_client import GitHubError
from llm import LLM

_REF = re.compile(r"^\$([a-zA-Z0-9_]+)(?:\.(.+))?$")


class Executor:
    def __init__(self, llm: LLM, github, memory, synthesizer: Synthesizer) -> None:
        self.llm = llm
        self.github = github
        self.memory = memory
        self.synth = synthesizer

    # ------------------------------------------------------------------ #
    def execute(self, plan: Plan):
        ctx: dict[str, object] = {}      # step id -> output
        results: list[StepResult] = []
        decisions: list[str] = []
        self._effects: list[dict] = []   # mutations made this run (for rollback)
        aborted = False

        for step in plan.steps:
            if aborted:
                results.append(StepResult(step, "skipped",
                                          error="Upstream step failed."))
                continue

            before = self.github.snapshot()
            try:
                output, status, decision = self._run_step(step, ctx, decisions)
                ctx[step.id] = output
                results.append(StepResult(
                    step, status, output=output,
                    api_calls=self.github.snapshot() - before, decision=decision))
            except _StepFailed as fail:
                results.append(StepResult(
                    step, "failed", error=str(fail),
                    api_calls=self.github.snapshot() - before, decision=fail.decision))
                decisions.append(f"{step.id}: {fail.decision or str(fail)}")
                aborted = True  # linear pipeline: stop, never half-complete silently
            except Exception as exc:  # belt-and-suspenders: never crash the agent
                results.append(StepResult(
                    step, "failed", error=f"unexpected error: {exc}",
                    api_calls=self.github.snapshot() - before,
                    decision="unhandled exception, step aborted"))
                decisions.append(f"{step.id}: unexpected error: {exc}")
                aborted = True

        return results, self._overall(results), decisions, self._effects

    # ------------------------------------------------------------------ #
    def _exec_and_record(self, cap: dict, args: dict):
        """Run a descriptor and, if it mutated state, log a rollback effect."""
        result = run_descriptor(self.github, cap["descriptor"], args)
        method = cap["descriptor"].get("method", "GET").upper()
        if method in ("POST", "PATCH", "PUT", "DELETE"):
            self._effects.append({
                "capability": cap["name"],
                "method": method,
                "args": args,
                "result": _compact(result),
            })
        return result

    def _run_step(self, step: Step, ctx: dict, decisions: list):
        args = self._resolve(step.args, ctx, item=None)

        if "for_each" in args:                       # fan-out over a list
            items = args.pop("for_each")
            if not isinstance(items, list):
                raise _StepFailed(f"for_each did not resolve to a list: {items!r}")
            outputs, failures = [], 0
            for it in items:
                per_args = self._resolve(step.args, ctx, item=it)
                per_args.pop("for_each", None)
                # Auto-expose the item's own scalar fields (e.g. an issue's
                # `number`) so a step or a synthesised descriptor can use them
                # even if the planner under-specified the per-item args.
                if isinstance(it, dict):
                    merged = {k: v for k, v in it.items()
                              if isinstance(v, (str, int, float, bool))}
                    merged.update(per_args)   # explicit args win
                    per_args = merged
                try:
                    outputs.append(self._invoke(step, per_args, decisions))
                except _StepFailed as f:
                    failures += 1
                    decisions.append(f"{step.id}[item]: {f.decision or f}")
            if failures and failures == len(items):
                raise _StepFailed(f"All {len(items)} items failed.",
                                  decision="entire fan-out failed")
            decision = (f"{len(outputs)}/{len(items)} items succeeded"
                        if failures else None)
            return outputs, "ok", decision

        out = self._invoke(step, args, decisions)
        return out, ("synthesized" if step.expect_synthesis else "ok"), None

    # ------------------------------------------------------------------ #
    def _invoke(self, step: Step, args: dict, decisions: list):
        cap = self.memory.capabilities.get(step.capability)
        if cap is None:
            return self._synthesize_and_run(step, args, decisions)
        if cap["kind"] == "transform":
            return run_transform(cap["name"], args)
        return self._run_api(cap, args, decisions, allow_recovery=True)

    # ---- capability gap: build -> test by real execution -> register ---- #
    def _synthesize_and_run(self, step: Step, args: dict, decisions: list):
        attempts: list[dict] = []
        for _ in range(MAX_SYNTHESIS_ATTEMPTS):
            cap = self.synth.build(step.description or step.capability,
                                   args, step.capability, attempts)
            t0 = time.time()
            try:
                out = self._call_with_recovery(cap, args, decisions)
            except GitHubError as err:
                attempts.append({"descriptor": cap["descriptor"],
                                 "error_kind": err.kind, "error": err.message})
                if err.kind in ("rate_limit", "permission"):
                    break  # refining the descriptor cannot fix these
                continue   # structural error -> refine and retry
            except (KeyError, ValueError, TypeError) as err:
                # Malformed descriptor or a path token with no matching arg —
                # feed it back so the next attempt fixes the descriptor.
                attempts.append({"descriptor": cap["descriptor"],
                                 "error": f"descriptor/args mismatch: {err}"})
                continue
            # Success: register the capability (and the constraints we discovered).
            for a in attempts:
                self._remember(cap, f"Resolved during synthesis: {a['error']}")
            self.memory.capabilities.register(cap)
            self.memory.capabilities.record_use(cap["name"], True,
                                                 (time.time() - t0) * 1000)
            decisions.append(
                f"{step.id}: synthesised capability '{cap['name']}'"
                + (f" after {len(attempts)} refinement(s)" if attempts else ""))
            step.capability = cap["name"]
            step.expect_synthesis = True
            return out
        last = attempts[-1]["error"] if attempts else "no attempts"
        raise _StepFailed(f"Could not build '{step.capability}': {last}",
                          decision=f"synthesis failed after {len(attempts)} attempts")

    # ---- known capability ---- #
    def _run_api(self, cap: dict, args: dict, decisions: list, allow_recovery: bool):
        t0 = time.time()
        try:
            out = self._exec_and_record(cap, args)
            self.memory.capabilities.record_use(cap["name"], True,
                                                 (time.time() - t0) * 1000)
            return out
        except (KeyError, ValueError, TypeError) as err:
            self.memory.capabilities.record_use(cap["name"], False,
                                                 (time.time() - t0) * 1000)
            raise _StepFailed(f"descriptor/args mismatch: {err}",
                              decision="capability args did not match its descriptor")
        except GitHubError as err:
            self.memory.capabilities.record_use(cap["name"], False,
                                                 (time.time() - t0) * 1000)
            self._remember(cap, f"{err.kind}: {err.message}")
            if err.kind == "validation" and allow_recovery:
                fixed = self._recover(cap, args, err, decisions)
                if fixed is not None:
                    return self._run_api(cap, fixed, decisions, allow_recovery=False)
            raise _StepFailed(str(err), decision=f"{err.kind} not recoverable")

    def _call_with_recovery(self, cap: dict, args: dict, decisions: list):
        """Run a descriptor; on a validation error, recover once then retry.
        Raises GitHubError if it still fails (so the synth loop can refine)."""
        try:
            return self._exec_and_record(cap, args)
        except GitHubError as err:
            if err.kind == "validation":
                fixed = self._recover(cap, args, err, decisions)
                if fixed is not None:
                    return self._exec_and_record(cap, fixed)
            raise

    # ------------------------------------------------------------------ #
    def _recover(self, cap, args, err, decisions):
        """Try to make a validation failure succeed. Returns the args to retry
        with (possibly modified), or None to give up.

        Tier 1 — LLM prerequisite: for 'missing entity' errors (e.g. a label that
        does not exist) we create the prerequisite and retry with the SAME args.
        Tier 2 — deterministic & safe: otherwise GitHub named an invalid optional
        field (e.g. bad assignees); drop it and retry. No mutation, no LLM, and
        no hallucinating fixes for format errors.
        """
        if self._looks_like_missing_entity(err) and \
                self._attempt_recovery(cap, args, err, decisions):
            return args
        fixed = self._drop_bad_fields(args, err)
        if fixed is not None:
            self._remember(cap, f"Drop invalid field(s) before retry: {err.message[:80]}")
            decisions.append(f"recovery: dropped invalid field(s), retried '{cap['name']}'")
            return fixed
        return None

    @staticmethod
    def _drop_bad_fields(args, err):
        body = err.body if isinstance(err.body, dict) else {}
        bad = {e.get("field") for e in (body.get("errors") or []) if e.get("field")}
        bad |= set(re.findall(r"properties/(\w+)", err.message or ""))
        droppable = (bad & set(args)) - {"title", "issue_number", "owner", "repo"}
        if not droppable:
            return None
        return {k: v for k, v in args.items() if k not in droppable}

    @staticmethod
    def _looks_like_missing_entity(err) -> bool:
        msg = (err.message or "").lower()
        return any(s in msg for s in
                   ("does not exist", "not found", "could not be found", "no such"))

    def _attempt_recovery(self, cap, args, err, decisions) -> bool:
        """Ask for ONE prerequisite action that would unblock the failed call."""
        system = (
            "A GitHub API call failed with a validation error. Propose AT MOST "
            "one prerequisite action that, run first, would let it succeed "
            "(e.g. create a label before applying it). If nothing would help, "
            'return {"action": null}. Respond ONLY as JSON: '
            '{"action": {"capability","args","description","expect_synthesis"}'
            '|null, "constraint": "short reusable rule"}'
        )
        user = json.dumps({"failed_capability": cap["name"], "failed_args": args,
                           "error_kind": err.kind, "error": err.message},
                          indent=2, default=str)
        try:
            plan = self.llm.complete_json(system, user, max_tokens=800)
        except Exception:
            return False

        if plan.get("constraint"):
            self._remember(cap, plan["constraint"])
        action = plan.get("action")
        if not action:
            return False

        pre = Step(id="recover", capability=action["capability"],
                   args=action.get("args", {}),
                   description=action.get("description", ""),
                   expect_synthesis=action.get("expect_synthesis", False))
        try:
            self._invoke(pre, self._resolve(pre.args, {}, item=None), decisions)
            decisions.append(f"recovery: ran prerequisite '{pre.capability}' "
                             f"then retried '{cap['name']}'")
            return True
        except _StepFailed:
            return False

    # ------------------------------------------------------------------ #
    def _remember(self, cap: dict, text: str) -> None:
        """Persist a constraint, whether or not the capability is registered yet."""
        constraints = cap.setdefault("constraints", [])
        if text not in constraints:
            constraints.append(text)
        if self.memory.capabilities.has(cap["name"]):
            self.memory.capabilities.save()

    def _resolve(self, value, ctx: dict, item):
        """Replace $sN / $sN.field / $item references inside args."""
        if isinstance(value, str):
            m = _REF.match(value)
            if not m:
                return value
            root, path = m.group(1), m.group(2)
            base = item if root == "item" else ctx.get(root)
            return _dig(base, path) if path else base
        if isinstance(value, dict):
            return {k: self._resolve(v, ctx, item) for k, v in value.items()}
        if isinstance(value, list):
            return [self._resolve(v, ctx, item) for v in value]
        return value

    @staticmethod
    def _overall(results) -> str:
        statuses = {r.status for r in results}
        if "failed" in statuses or "skipped" in statuses:
            ok = any(r.status in ("ok", "synthesized") for r in results)
            return "partial" if ok else "failed"
        return "success"


def _compact(result):
    """Keep only the fields needed to invert a mutation (ids, numbers, names)."""
    if isinstance(result, dict):
        keep = ("number", "id", "name", "state", "html_url", "title")
        out = {k: result[k] for k in keep if k in result}
        if "labels" in result and isinstance(result["labels"], list):
            out["labels"] = [l.get("name") if isinstance(l, dict) else l
                             for l in result["labels"]]
        return out or result
    if isinstance(result, list):
        return [_compact(r) for r in result]
    return result


def _dig(obj, path):
    for part in path.split("."):
        if isinstance(obj, dict):
            obj = obj.get(part)
        elif isinstance(obj, list):
            obj = [(_x.get(part) if isinstance(_x, dict) else None) for _x in obj]
        else:
            return None
    return obj


class _StepFailed(Exception):
    def __init__(self, message: str, decision: str | None = None) -> None:
        super().__init__(message)
        self.decision = decision
