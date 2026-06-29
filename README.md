# Autonomous Platform Intelligence Agent — GitHub

A natural-language agent for GitHub that **plans, executes, remembers, and
improves**. Give it an instruction; it decomposes the task, calls the real
GitHub API, synthesises any operation it doesn't yet have, recovers from
failures, and stores what it learned so the next run is cheaper and more
reliable.

Built for the Watermelon Software "Autonomous Platform Intelligence Agent"
assignment. See [ARCHITECTURE.md](ARCHITECTURE.md) (the three required answers)
and [DEMO.md](DEMO.md) (the three live instructions).

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env          # fill in GITHUB_TOKEN, GITHUB_OWNER, GITHUB_REPO, GROQ_API_KEY

python main.py "Create an issue titled 'Login times out' describing the bug, and label it 'bug'."
python demo.py                # the full 3-instruction walkthrough, run twice each
python dashboard.py           # web UI at http://localhost:8765 (stdlib only, no extra deps)
```

### Web dashboard

`python dashboard.py` serves a one-page UI at `http://localhost:8765` that wraps
the **same** `Agent` as the CLI — chatbox → run, plus one-click rollback. It
foregrounds exactly what the agent is judged on: the structured execution
report, the **memory before → after diff**, the live **capability memory**
(builtin vs synthesised, success rates, constraints), and the
**run-1-vs-run-N learning curve**. It is built on Python's standard-library
`http.server` only — no Flask/FastAPI, nothing new to justify.

> Use a **throwaway repo** — the agent makes real, mutating API calls.
> A classic PAT with `repo` scope (or a fine-grained token with Issues +
> Contents read/write) is enough.

## CLI

| Command | What it does |
|---|---|
| `python main.py "<instruction>"` | Run the agent; prints a structured execution report + what memory learned. |
| `python main.py "<instruction>" --auto-rollback` | Run, and if it half-fails, automatically undo the mutations it made. |
| `python main.py --rollback [exec_id]` | Undo a past run's mutations (closes created issues, removes applied labels, …) by synthesising the inverse ops. |
| `python main.py --show-memory` | Inspect both memory layers: capabilities, success rates, discovered constraints, recent runs. |
| `python main.py --stats "<instruction>"` | The learning curve for that instruction pattern: API calls / LLM calls / duration, run by run. |
| `python main.py --reset-memory` | Explicit wipe. **Never** happens automatically. |
| `python demo.py [--once]` | Scripted 3-instruction demo (twice each by default). |

## How it works (one paragraph)

The **planner** checks execution memory first — a repeated instruction replays a
stored, improved plan with zero planning LLM calls. Otherwise an LLM decomposes
it against the **capability catalog**. The **executor** runs each step:
in-process transforms (filter/group/count/format) or real API calls via a
declarative descriptor. A missing capability is **synthesised at runtime**
(reason → build descriptor → test against the live API → refine on structural
errors → recover from missing prerequisites → register). Every API failure
becomes a durable **constraint** in capability memory. After a run that did real
work, the agent **reflects**: it records reusable hints and an improved plan
under the instruction's signature — which is why run N beats run 1.

## Layout

```
main.py            CLI + report rendering
demo.py            scripted 3-instruction walkthrough
config.py          env / .env loading
llm.py             Anthropic wrapper (counts LLM calls)
github_client.py   the ONE hand-written primitive: raw GitHub REST + error classification
agent/
  core.py          orchestration + reflection (the learning loop)
  planner.py       memory-aware decomposition
  executor.py      step execution, synthesis test/refine, recovery, partial-failure
  synthesis.py     LLM reasoning that builds capability descriptors
  descriptor.py    safe interpreter for a declarative API descriptor
  transforms.py    in-process data transforms (no API cost)
  models.py        Plan / Step / StepResult / ExecutionReport
memory/
  store.py         CapabilityMemory + ExecutionMemory (+ snapshots)
memory_data/       persisted JSON (gitignored; survives across runs)
```

## Dependencies (and why)

- **httpx** — both the GitHub REST calls and the LLM calls (Groq's
  OpenAI-compatible endpoint). One HTTP client, no provider SDK to justify.
- **python-dotenv** — load secrets from `.env`.
- **rich** — readable execution reports / memory tables in the terminal.

LLM provider is **Groq** (default model `llama-3.3-70b-versatile`) via its
OpenAI-compatible API — swapping providers is just `LLM_BASE_URL` + `LLM_MODEL`
+ key. No agent framework, no vector DB, no platform automation shortcuts (no
Zapier) — the memory, synthesis, and learning are implemented directly so each
can be reasoned about.

## Notes / honest limitations

- Plan reuse keys on a normalised instruction string + an LLM-derived intent
  signature; paraphrase robustness would improve with embedding-based
  signatures (noted in ARCHITECTURE.md → "what I'd build next").
- The linear executor stops on the first hard failure and reports remaining
  steps as skipped — deliberately, to avoid silent half-completions. Independent
  parallel branches aren't scheduled concurrently.
- Synthesised capabilities are declarative API descriptors (safe, no code
  execution). Transformations beyond the built-in set would be the next
  synthesis target.
