# Demo

Three instructions of increasing complexity, run **live** on a real GitHub repo.
Memory persists across all of them — nothing is wiped.

```bash
python demo.py            # runs all three, twice each, and prints the learning curve
# or drive them one at a time:
python main.py "<instruction>"
python main.py --stats "<instruction>"     # before/after numbers
python main.py --show-memory               # inspect both memory layers
```

> Set up a throwaway repo first (`GITHUB_OWNER`/`GITHUB_REPO` in `.env`) — the
> agent makes real, mutating API calls.

---

## Instruction 1 — simple (builtin path)

> *"Create an issue titled 'Login times out after 30s' describing that users are
> logged out mid-session, and label it 'bug'."*

**Expected:** one `create_issue` call (a builtin) with title, body, and the
`bug` label. Returns a structured report with the new issue number. Demonstrates
the end-to-end NL → API path and the execution report. No synthesis needed.

## Instruction 2 — medium (capability synthesis + recovery)

> *"Find every open issue that has no assignee and add the label 'needs-triage'
> to each of them."*

**Expected, run 1:** decompose into `list_issues` → `filter (unassigned)` →
fan-out `add_label_to_issue`. `add_label_to_issue` **does not exist**, so the
agent synthesises it. The first apply fails 422 because the `needs-triage` label
doesn't exist; the agent **discovers this constraint**, synthesises
`create_label`, creates it, retries, and finishes. It then reflects and stores
an improved plan.

**Expected, run 2 (same instruction):** plan replayed from memory — **0 planning
LLM calls**, label created up front (no failed 422), unassigned issues fetched
server-side. Fewer API calls, fewer/zero LLM calls. `--stats` shows the drop.

## Instruction 3 — compound (multi-step decomposition + transforms)

> *"Find all open issues, group them by label, and create a single summary issue
> titled 'Weekly Triage Summary' listing how many open issues fall under each
> label."*

**Expected:** `list_issues` → `count_by (labels.name)` → `format_template`
(render a markdown table of label → count) → `create_issue` (the summary).
Demonstrates compound decomposition, in-process transforms composed with real
API calls, and partial-failure handling (if the summary `create_issue` fails,
earlier steps are reported as done and the summary step is reported failed — no
silent half-completion).

---

## What to point at on the call

- **Memory before/after** for instruction 2: capability count grows by
  `add_label_to_issue` + `create_label`; a new constraint appears
  ("label must exist before applying"); a learned note is attached to the
  pattern. (`--show-memory`)
- **The numbers** from `--stats`: run 1 vs run 2 API calls and LLM calls for
  instruction 2 — concretely *"run 1 made N calls and M LLM calls; run 2 made
  fewer of each because the agent folded the create-label prerequisite into the
  plan and reused the synthesised capability."*
- **A real failure**: temporarily revoke the token's write scope and run
  instruction 1 to show the structured partial-failure report and the
  `permission` constraint being recorded.
- **Rollback** (bonus depth): after instruction 1, run `python main.py
  --rollback`. The agent reasons that the inverse of "create issue" is "close
  it", **synthesises a `close_issue` capability on the spot**, closes the issue,
  and marks the run `rolled_back`. Verified live on this repo (issue #10).
