"""Runtime capability synthesis — the *reasoning/generation* half.

`build()` turns a described operation into a declarative API descriptor (one LLM
call). It deliberately does NOT execute anything: testing-by-real-execution and
the refine-on-error loop live in the executor, so a synthesised capability is
tested through exactly the same recovery machinery as any other call (e.g. if
applying a label fails because the label doesn't exist, the executor creates it
and retries — synthesis shouldn't have to special-case that).

The loop is: executor calls build() -> runs the descriptor for real -> on a
*structural* failure (wrong path/method/field) it calls build() again with the
error attached, up to N times -> on success the capability is registered.
"""
from __future__ import annotations

import json

from llm import LLM

SYSTEM = (
    "You build ONE GitHub REST API capability as a declarative descriptor. "
    "You are given the operation needed and the runtime arguments available.\n\n"
    "Return ONLY JSON:\n"
    "{\n"
    '  "name": "snake_case_name",\n'
    '  "description": "what it does + arg contract",\n'
    '  "descriptor": {\n'
    '    "method": "GET|POST|PATCH|PUT|DELETE",\n'
    '    "path": "/repos/{owner}/{repo}/...",  // {owner},{repo} auto-filled; '
    "other {tokens} pull from args\n"
    '    "query": ["argName", ...],            // args copied to the query string\n'
    '    "body": {"apiField": "argName"},      // map API body fields to arg names\n'
    '    "extract": null                        // optional dotted response path\n'
    "  }\n"
    "}\n\n"
    "Rules: use the documented GitHub REST v3 endpoint. Only reference arg names "
    "that exist in the provided args. If a previous attempt failed, READ the "
    "GitHub error and fix the descriptor (wrong path, wrong method, wrong field "
    "name). A validation error about a missing *value* (e.g. a label that does "
    "not exist yet) means the descriptor is correct — keep it as-is."
)


class Synthesizer:
    def __init__(self, llm: LLM, github, capabilities) -> None:
        self.llm = llm
        self.github = github
        self.capabilities = capabilities

    def build(self, need: str, args: dict, proposed_name: str | None,
              prior_attempts: list[dict]) -> dict:
        """Generate (or refine) a capability descriptor. Pure reasoning, no call."""
        user = json.dumps({
            "operation_needed": need,
            "proposed_name": proposed_name,
            "available_args": {k: _describe(v) for k, v in args.items()},
            "previous_failed_attempts": prior_attempts,
        }, indent=2, default=str)
        spec = self.llm.complete_json(SYSTEM, user, max_tokens=1500)
        return {
            "name": spec.get("name") or proposed_name or "synthesized_op",
            "kind": "api",
            "origin": "synthesized",
            "description": spec.get("description", need),
            "descriptor": spec["descriptor"],
            "constraints": [],
        }


def _describe(value):
    """Compact type/shape hint so the LLM knows the arg contract without bulk."""
    if isinstance(value, list):
        sample = value[0] if value else None
        return {"type": "list", "len": len(value), "sample": _describe(sample)}
    if isinstance(value, dict):
        return {"type": "dict", "keys": list(value)[:8]}
    return {"type": type(value).__name__, "value": value}
