---
name: projectlayer-build
description: >-
  Works with development plans on ProjectLayer.app tasks via its REST API — both
  authoring a plan from a task's description and building the code from a plan. Use this
  whenever the user references ProjectLayer, a ProjectLayer task or task key (e.g.
  "API-42", "PL-17"), a "build plan" or "claude_plan", or asks to plan/build/implement a
  task that lives in ProjectLayer — even without naming the API (e.g. "write a plan for
  ticket 42", "draft a plan from the description and push it back", "implement the next
  planned task", "pull my plan and start coding"). It reads projects/tasks/descriptions,
  drafts a plan and writes it back to the task, builds the code locally, and keeps the
  task's status in sync (in_progress when work begins, done once built and verified).
compatibility: Requires the PROJECTLAYER_API_TOKEN environment variable and network access to projectlayer.app.
---

# ProjectLayer Build

This skill covers the full plan lifecycle on a ProjectLayer task, in two flows:

- **Author a plan** — pull a task's description, draft a build plan from it, and write the
  plan back onto the task.
- **Build from a plan** — take a task's plan and turn it into working code in this repo,
  keeping the task status in sync.

They chain naturally (author, then build) or run independently. Throughout, ProjectLayer
holds the *what* and a rough *how*; the repository is the source of truth for *how it's
actually done here* — reconcile the two rather than following either blindly.

## Which flow?

- "write/draft/generate a plan for <task>", "plan out ticket 42", "turn the description
  into a plan", "...and push it back to ProjectLayer" → **Author a plan**.
- "build/implement/work through <task>'s plan", "start coding the next planned task" →
  **Build from a plan**.
- "plan and build <task>" → do **Author** first, then **Build** using the plan you just
  wrote (you already have it in hand, so no need to re-fetch).

## Prerequisites

The bundled script talks to the API. It needs a token:

```bash
export PROJECTLAYER_API_TOKEN=pl_live_xxx   # scoped API token from ProjectLayer → Settings
```

If `PROJECTLAYER_API_TOKEN` is unset, the script says so with instructions — surface that
to the user rather than trying to guess a token. The plugin targets the hosted ProjectLayer
SaaS at `https://projectlayer.app` only; the API base URL is fixed and not configurable.

The write operations (`set-plan`, `update-status`) require a token with the **write**
scope. If one comes back `403 Forbidden`, the token is read-only — tell the user to issue a
write-scoped token rather than retrying.

All API access goes through the bundled script so you don't re-derive auth and error
handling each time. From the skill directory:

```bash
python scripts/projectlayer.py list-projects
python scripts/projectlayer.py list-tasks [--project N] [--has-plan] [--status STATUS]
python scripts/projectlayer.py get-task <id-or-task-key>          # e.g. 42 or API-42
python scripts/projectlayer.py update-status <id-or-task-key> <open|in_progress|done|closed>
python scripts/projectlayer.py set-plan <id-or-task-key> --file plan.txt   # or pipe via stdin
```

Each prints JSON. On error it prints a clear message to stderr and exits non-zero.
`update-status` and `set-plan` are the write operations; the rest are read-only.

## Identify the task (both flows start here)

Map what the user said to a specific task id:

- **They gave a task key or id** ("plan API-42", "build task 42") → go straight to
  `get-task API-42`.
- **They named a project or were vague** ("plan the next open task", "start on the export
  work") → `list-projects` to find the project, then `list-tasks --project N` (add
  `--has-plan` when building, since building needs a plan). Show the candidates and confirm
  which one before doing real work — guessing wastes effort.

---

## Flow A — Author a plan

Use this when the task has a description but no good plan yet (`has_plan: false`, or the
user wants a fresh plan). The goal is a clear, buildable plan derived from what the task
actually asks for.

### A1. Fetch and understand the description

`get-task <id>` returns the task. The `description` is **HTML** (e.g. `<ol><li>...</li></ol>`)
— read it as rendered content and extract the real requirements, don't treat tags as text.
Read the `title` too. If the description is thin or ambiguous, ask the user to clarify
rather than inventing scope — a plan built on guesses wastes the build that follows.

### A2. Draft the plan

Write the plan as **numbered steps in Markdown** — `claude_plan` is stored as Markdown, so
you can use headings, bold, and fenced code where they aid clarity, but keep the backbone a
numbered list since that's what the Build flow works through:

```markdown
1. Add the export endpoint...
2. Stream rows...
3. Verify with a functional test.
```

Good plans: ordered so each step builds on the last; concrete about what changes; end with
a verification step. Aim for steps a developer could follow without re-deriving the whole
design — but don't over-specify implementation the codebase should decide. If you have this
repo available, a quick look at how similar features are built makes the plan land better.

Show the draft to the user before pushing it back, unless they've said to just do it. The
plan is cheap to adjust now and expensive to redo after building.

### A3. Push the plan back to ProjectLayer

Write the plan Markdown to a file, then set it on the task:

```bash
python scripts/projectlayer.py set-plan <id-or-key> --file /tmp/plan.md
```

This PATCHes the task's `claude_plan` field (and nothing else); the server stamps
`plan_updated_at` and flips `has_plan` to true. `set-plan` refuses an empty plan and
verifies the plan actually stored — it fails loudly if the response comes back without it.
On a `403`, the token lacks the write scope (see Prerequisites). If a write fails for any
reason, the plan text isn't lost — surface it to the user so nothing is wasted.

From here you can hand off to **Flow B** to build it — you already have the plan, so no need
to re-fetch.

---

## Flow B — Build from a plan

### B1. Fetch and read the plan

`get-task <id>` returns the task, including `claude_plan`. That field is **Markdown**, and
in practice reads as numbered steps, e.g.:

```markdown
1. Add the export endpoint...
2. Stream rows...
3. Verify with a functional test.
```

Read the whole plan *and* the task `title`/`description` before writing anything. The plan
is a sketch, not a spec — it was written without deep knowledge of this repo's current
state. Treat surprising or stale-sounding steps with healthy skepticism and check them
against the actual code.

Note the field formats returned by the API:
- `description` is **HTML** (e.g. `<ol><li>...</li></ol>`). Read it as rendered content —
  interpret the markup and requirements, don't treat the tags as literal text.
- `claude_plan` is **Markdown** (numbered steps in practice), or `null` if no plan exists yet.
- `has_plan: false` / `claude_plan: null` means there's no plan to build from. Don't guess
  one inline — switch to **Flow A** to author a plan (from the description) and write it
  back first, then build. Confirm with the user before doing so if scope is unclear.

### B2. Orient in the codebase

Before implementing, spend a moment learning how this repo does the relevant thing: where
similar features live, the test framework, naming and file conventions, how routes/modules
are wired. Code you write should read like it was always there. This step is what separates
"followed the plan" from "shipped something that fits."

### B3. Mark the task in progress

Once you've confirmed the task and are about to start building, reflect that in
ProjectLayer so the board and the assignee/reporter stay in sync:

```bash
python scripts/projectlayer.py update-status <id-or-key> in_progress
```

Do this only when you're genuinely starting the work, not while still deciding which task
to build — the change is logged in the task's activity feed and notifies people. If the
task is already `in_progress`, skip it. If the status update fails (e.g. a permissions
error), say so but keep building; a failed status sync shouldn't block the actual work.

### B4. Build, step by step

Work through the numbered steps in order, because later steps usually assume earlier ones
exist. For each step:

- Implement the smallest coherent piece that satisfies the step.
- Where the plan calls for verification (many plans end with a "verify"/"test" step), or
  where the change has real runtime behavior, **write or run a test / exercise the code** —
  don't just assert it works. A plan that says "verify with a functional test" means it.
- If a step is already done, no longer applies, or is wrong for this codebase, don't force
  it. Note the deviation and why, and keep going. Faithfulness to the plan's *goal* beats
  literal step-by-step compliance.

If you have the test-driven-development or systematic-debugging skills available and the
work fits them, use them — they compose naturally with plan-driven building.

### B5. Mark done and report back

Only after the plan is genuinely built **and verified** (tests pass, behavior observed —
not just "the code looks right"), mark the task done:

```bash
python scripts/projectlayer.py update-status <id-or-key> done
```

Setting `done` stamps the task's completion time in ProjectLayer. Be honest about this
gate: if you skipped steps, couldn't verify, or left follow-up work, the task isn't done —
leave it `in_progress`, tell the user what's outstanding, and let them decide. Don't mark
something done to tidy up the board when it isn't finished.

Then summarize concisely:

- What you built, mapped back to the plan's steps (which are done, which you skipped/changed
  and why).
- What you verified and how (tests run, output observed).
- The task's new status in ProjectLayer, and anything the plan implied that still needs a
  human decision or follow-up.

## Notes

- Task statuses (the exact strings the API expects) are `open`, `in_progress`, `done`,
  `closed`. Use `--status` on `list-tasks` to filter (e.g. find what's still open with a
  plan). An unknown status is rejected with a 400.
- Write operations:
  - `update-status` → `PATCH /api/v1/tasks/{id}/status`. Setting `done`/`closed` records a
    completion time and notifies the assignee and reporter, so only use it when the state is
    real — don't churn the activity log with speculative flips.
  - `set-plan` → `PATCH /api/v1/tasks/{id}` with `{"claude_plan": <markdown>}`, writing only
    the plan field. Storing a plan stamps `plan_updated_at`, flips `has_plan` to true, and
    clears any in-progress/errored planning state; sending an empty string would clear the
    plan, which is why `set-plan` refuses empty input. Requires the write scope.
- `list-tasks` returns up to 100 tasks (newest first) and does **not** include the plan text —
  only `get-task` does. So: list to find the id, then get to read the plan.
- Keep the API interaction to the bundled script. If you hit an endpoint the script doesn't
  cover, prefer extending the script over ad-hoc `curl`, so error handling stays consistent.
