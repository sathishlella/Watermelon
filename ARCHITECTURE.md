# Architecture

An autonomous agent for **GitHub**: it takes a natural-language instruction,
decomposes it, executes it via real GitHub REST calls, and gets cheaper and more
reliable each time it sees the same kind of work — because it remembers what it
learned and replays a better plan.

```
instruction → Planner ──(memory hit? replay stored plan, 0 LLM calls)
                 │ else LLM-decompose
                 ▼
             Executor ─ per step ─► transform (in-process)
                                  └► API capability? run it ; if missing →
                                     Synthesiser: reason→build→TEST→register
                                     (refine on structural error; recover a
                                      missing prerequisite, then retry)
                 ▼
             Reflection → learned notes + an improved plan, stored by signature
```

## 1. What memory stores, and why it's structured that way

Two JSON layers in `memory_data/` (inspectable via `--show-memory`, never
auto-wiped). It is structured knowledge the planner/executor *read to decide* —
not a log, and not embedded prompts retrieved by similarity.

- **Execution memory** — one record per run: the instruction, a normalised
  string, a canonical **intent signature** (e.g. `label_unassigned_open_issues`),
  the **plan to replay next time**, outcome/metrics, and **learned notes**. The
  planner keys off this: an exact match replays the stored plan with *zero*
  planning LLM calls; a signature match adapts the prior plan.
- **Capability memory** — what the agent can *do*: each capability's descriptor,
  success/uses stats, origin (`builtin` vs `synthesized`), and **constraints
  discovered at runtime** (validation, permissions, rate limits).

The two things that change behaviour are *how to decompose a goal* and *how to
perform an operation + its limits* — so we store exactly those, keyed for direct
lookup. Only `list_issues`, `create_issue`, and pure transforms are seeded;
everything else (label, comment, assign, create-label, close, …) is synthesised.

## 2. How capability synthesis works

When a step needs a capability that doesn't exist, it is built **at runtime**:
an LLM proposes a *declarative descriptor* (method, path, query/body arg mapping)
— we never `exec` generated code, so the execution surface stays safe. The
descriptor is **tested by real execution** (the test *is* the call the task
needs). A structural failure (wrong path/method/field) is fed back and the
descriptor is refined, up to N attempts; a *precondition* failure (e.g. 422
"label does not exist") is fixed by synthesising and running the prerequisite
(`create_label`) and retrying. On success it is registered and reused forever;
after N failures the step fails with a clear report of what was tried.

## 3. The learning signal — run N vs run 1

Measured per run: **LLM calls**, **API calls**, and wall-clock (`--stats` prints
the curve). **Run 1** of `label_unassigned_open_issues` spends signature +
decomposition LLM calls, synthesises `add_label_to_issue`, hits a 422 (label
missing), synthesises+runs `create_label`, retries, then **reflects** — storing
the note *"ensure the label exists first"* and rewriting the plan to filter
unassigned server-side and create the label up front. **Run N** finds that
improved plan by exact signature match → **0 planning/synthesis LLM calls**, the
synthesised capabilities already exist, and the failed-422-then-recover
round-trip is gone. Live this collapses LLM calls to **0** on reuse (offline
harness: **api 5→4, llm 5→0**). Same instruction, fewer calls, no rediscovered
failures — behaviour changed by what was learned, not by adding examples.

## Rollback (implemented)

Every mutating call is logged as a reversible *effect*. `--rollback` (or
`--auto-rollback` on a partial run) asks the LLM to reason out the compensating
actions in reverse and runs them through the same executor+synthesis path — so
inverse ops (`close_issue`, `remove_label_from_issue`) are synthesised on demand.
Verified live (a created issue is closed; the run is marked `rolled_back`).

## What I'd build next

**Confidence scoring** (down-weight low-success capabilities, re-plan when
unsure); **memory compaction** (summarise old records per signature instead of
appending); **semantic signatures** (embeddings) so paraphrases hit the reuse
path more often than the current normalised-string + LLM signature.

*LLM provider is OpenAI-compatible and swappable via `.env` (default
`gpt-4.1-mini`); the GitHub client is the one hand-written primitive.*
