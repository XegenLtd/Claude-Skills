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
  drafts a plan and writes it back to the task, builds the code locally, writes
  plain-language test steps and hands the task over for testing, fixes tasks that
  failed testing, and keeps the task's status in sync (in_progress while building,
  testing once built and verified).
compatibility: Requires the PROJECTLAYER_API_TOKEN environment variable and network access to projectlayer.app.
---

# ProjectLayer Build

This skill covers the full plan lifecycle on a ProjectLayer task:

- **Author a plan** — pull a task's description, draft a build plan from it, and write the
  plan back onto the task.
- **Build from a plan** — take a task's plan and turn it into working code in this repo,
  then write test steps and hand the task over for testing.
- **Fix a failed test** — pick up a task a tester failed, fix what they found, and send it
  back for another round.
- **Run the tests** — only when the user asks: work through the test steps yourself and
  record the results.

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
- "fix the failed test on <task>", "what failed testing?", "pick up the failed tasks" →
  **Fix a failed test**.
- "run the tests for <task>", "test it yourself and sign it off" → **Run the tests**.

## Prerequisites

The bundled script talks to the API. It needs a write-scoped ProjectLayer API token, which
it reads from either of these (checked in this order):

1. `PROJECTLAYER_API_TOKEN` — an environment variable (for CLI/CI or manual use).
2. `CLAUDE_PLUGIN_OPTION_PROJECTLAYER_API_TOKEN` — injected automatically by Claude Code
   when the plugin is installed: the user is prompted for the token on enable and it's
   stored securely in the OS keychain. Normal users don't set anything by hand.

If neither is present the script explains how to supply one — surface that to the user
rather than trying to guess a token. If you installed the plugin but were never prompted,
re-enable it (`/plugin` → Installed → enable) to trigger the config prompt. Tokens are
issued from **Settings → API Keys** in the ProjectLayer portal.

The plugin targets the hosted ProjectLayer SaaS at `https://projectlayer.app` only; the API
base URL is fixed and not configurable.

The write operations require a token with the **write** scope. If one comes back
`403 Forbidden` saying the key "lacks the 'write' scope", the token is read-only — tell the
user to issue a write-scoped token rather than retrying.

The API key acts as the ProjectLayer user who created it, so everything you write
(statuses, test results, sign-offs) is recorded as that person.

All API access goes through the bundled script so you don't re-derive auth and error
handling each time. From the skill directory:

```bash
python scripts/projectlayer.py list-projects
python scripts/projectlayer.py list-tasks [--project N] [--has-plan] [--status S[,S...]] \
                                          [--test-outcome O] [--limit N]
python scripts/projectlayer.py get-task <id-or-task-key>          # e.g. 42 or API-42
python scripts/projectlayer.py update-status <id-or-task-key> <status>
python scripts/projectlayer.py set-plan <id-or-task-key> --file plan.md   # or pipe via stdin
python scripts/projectlayer.py get-test-steps <id-or-task-key>
python scripts/projectlayer.py set-test-steps <id-or-task-key> --file steps.json
python scripts/projectlayer.py list-test-runs <id-or-task-key>
python scripts/projectlayer.py get-test-run <id-or-task-key> [RUN_NUMBER|current]
python scripts/projectlayer.py record-result <id-or-task-key> <result-id> <pass|fail|pending> [--note TEXT]
python scripts/projectlayer.py sign-off <id-or-task-key>
```

Each prints JSON. On error it prints a clear message to stderr and exits non-zero; exit
code **3** means the ProjectLayer server doesn't support test steps yet (see B5).
`update-status`, `set-plan`, `set-test-steps`, `record-result` and `sign-off` are the write
operations; the rest are read-only. The list commands fetch every page, so what they
return is the complete list, not just the first page.

## Identify the task (both flows start here)

Map what the user said to a specific task id:

- **They gave a task key or id** ("plan API-42", "build task 42") → go straight to
  `get-task API-42`.
- **They named a project or were vague** ("plan the next open task", "start on the export
  work") → `list-projects` to find the project, then `list-tasks --project N` (add
  `--has-plan` when building, since building needs a plan, and `--status open,in_progress`
  to leave out finished work). Show the candidates and confirm
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

### B5. Write test steps and hand over for testing

Only after the plan is genuinely built **and verified** (tests pass, behavior observed —
not just "the code looks right"), hand the task over for testing. Be honest about this
gate: if you skipped steps, couldn't verify, or left follow-up work, the task isn't ready —
leave it `in_progress`, tell the user what's outstanding, and let them decide.

ProjectLayer tasks carry a **test script**: plain-language steps a non-technical person
works through in the UI, marking each one Pass or Fail. Write one that checks the work
from the user's point of view:

```json
[
  {"instruction": "Go to Projects and open any project. Click the Export button at the top of the task list.",
   "expected_result": "A file called tasks.csv downloads."},
  {"instruction": "Open tasks.csv in Excel or Numbers.",
   "expected_result": "There is one row per task, with the task key, title and status in the first three columns."}
]
```

Good steps:
- **One action per step.** If a step says "and then", split it.
- **Name the exact page, button or field**, using the words on screen.
- **Describe what the tester should see**, specifically enough to tell pass from fail.
- **No jargon** — no routes, endpoints, database terms or code names. Write for someone
  who has never seen the codebase.
- Cover what the task asked for, including the obvious edge case (empty list, wrong
  input), but stay under a dozen or so steps. The API allows at most 50, and each field up
  to 2000 characters.

Show the steps to the user before sending them, unless they've said to just do it. Then:

```bash
python scripts/projectlayer.py set-test-steps <id-or-key> --file /tmp/steps.json
python scripts/projectlayer.py update-status <id-or-key> testing
```

`set-test-steps` replaces the whole script. Moving to `testing` starts a new round with
every step pending, and notifies the assignee, reporter and watchers. Don't record results
or sign off yourself — a person does that, unless the user explicitly asks you to (see
**Run the tests**).

- `set-test-steps` returns **409** while the task is already in Testing, because the
  script is frozen under a tester. Tell the user rather than moving the task out of
  Testing to force it.
- **Exit code 3** means this ProjectLayer server doesn't support test steps yet. Fall back
  to marking the task done instead: `update-status <id-or-key> done`.
- Marking a task `done` returns **409** if it's blocked by open tasks; the message lists
  them. Leave it where it is and tell the user which tasks are blocking it.

### B6. Report back

Summarize concisely:

- What you built, mapped back to the plan's steps (which are done, which you skipped/changed
  and why).
- What you verified and how (tests run, output observed).
- The test steps you wrote, and the task's new status in ProjectLayer — plus anything the
  plan implied that still needs a human decision or follow-up.

---

## Flow C — Fix a failed test

When a tester fails a step, ProjectLayer closes that round as `failed`, moves the task back
to `in_progress`, and posts a comment quoting the step and what the tester saw.

### C1. Find the failure

- Given a task → `list-test-runs <id-or-key>`; the newest round is first.
- Otherwise → `list-tasks --status in_progress --test-outcome failed` (add `--project N` if
  they named one) lists every task whose latest round failed. Confirm which to work on.

Then `get-test-run <id-or-key> <run_number>` for the failed round. Its `results` show which
step failed and the tester's `note`; steps after it may still be `pending`, because a round
ends at the first fail.

### C2. Fix it

Re-read the task and plan, reproduce what the tester describes, and fix it — the
Build-flow rules (B2, B4) apply. The tester's note says what they *saw*, not the cause;
debug from there rather than patching the symptom.

### C3. Send it back for testing

If the fail showed the step itself was wrong or unclear (not the code), fix the step with
`set-test-steps` — the task is out of Testing now, so the script can be edited. Earlier
rounds keep their own copy, so history isn't affected. Then `update-status <id-or-key>
testing` to start a new round, which re-runs the whole script. Report what failed, what you
changed, and that it's back in testing.

---

## Flow D — Run the tests (only when asked)

Only do this when the user explicitly asks you to run the tests or sign off. A result or
sign-off you record is attributed to the API key's creator, not to a tester, so it
shouldn't happen by default.

1. `get-test-run <id-or-key> current` — the open round and its `results`, each with an
   `id`. (404 means the task isn't in Testing.)
2. Work through the steps in order, doing what each one says as closely as you can (run
   the app, call the page, check the output). Record each with `record-result <id-or-key>
   <result-id> pass`, or `fail --note "What happened instead"`. A fail ends the round and
   moves the task back to `in_progress` — switch to **Fix a failed test**. If you can't
   perform a step yourself (it needs a real device, a human judgment, an account you don't
   have), stop and tell the user rather than guessing a result.
3. When every step has passed, the result response says `all_passed: true`. Only `sign-off`
   if the user asked you to; it closes the round and marks the task done (409 if it's
   blocked by open tasks). Otherwise tell the user it's ready for them to sign off.

Setting a result back to `pending` undoes a mis-recorded one within the open round.

## Notes

- Task statuses (the exact strings the API expects) are `open`, `in_progress`, `on_hold`,
  `testing`, `done`, `closed`. `on_hold` means the work is paused — don't start building it
  without checking with the user. `testing` means it's with a tester. Use `--status` on
  `list-tasks` to filter, with several comma-separated (`--status open,in_progress`).
- `list-tasks --test-outcome` filters on the latest round's outcome (`in_progress`,
  `passed`, `failed`, `abandoned`).
- Write operations:
  - `update-status` → `PATCH /api/v1/tasks/{id}/status`. Every change is logged and
    notifies people, so only use it when the state is real — don't churn the activity log
    with speculative flips. `testing` needs test steps (409 without them). `done`/`closed`
    records a completion time, and returns 409 if the task has open blockers or is in
    Testing (a task in Testing is finished by a sign-off, not a status change).
  - `set-plan` → `PATCH /api/v1/tasks/{id}` with `{"claude_plan": <markdown>}`, writing only
    the plan field. Storing a plan stamps `plan_updated_at`, flips `has_plan` to true, and
    clears any in-progress/errored planning state; sending an empty string would clear the
    plan, which is why `set-plan` refuses empty input. Requires the write scope.
  - `set-test-steps` → `PUT /api/v1/tasks/{id}/test-steps`; `record-result` →
    `PATCH …/test-runs/current/results/{result-id}`; `sign-off` →
    `POST …/test-runs/current/sign-off`.
- Task keys can use **any prefix** — there's no default; the user chooses each project's
  prefix when creating it, so keys look like `API-42` or `ACME-3` but could be anything.
  Never assume a prefix — use the key the user gave, or look it up. Commands accept a numeric id or a key; the script matches the key
  case-insensitively and, since the API has no by-key lookup, finds the project with that
  prefix and pages through its tasks (or through every task, if no active project has the
  prefix). So a valid key resolves regardless of prefix or how far back the task is.
- `list-tasks` returns every matching task and does **not** include the plan text — only
  `get-task` does. So: list to find the id, then get to read the plan. Order: most recently
  updated first; by task number with `--project`; by latest plan with `--has-plan`.
- Keep the API interaction to the bundled script. If you hit an endpoint the script doesn't
  cover, prefer extending the script over ad-hoc `curl`, so error handling stays consistent.
