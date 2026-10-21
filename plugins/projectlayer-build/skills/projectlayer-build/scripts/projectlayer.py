#!/usr/bin/env python3
"""
Thin client for the ProjectLayer REST API.

Why this exists: every invocation of the projectlayer-build skill needs the same
handful of calls with the same auth header and the same error handling. Doing that
once here — with clear, actionable error messages — keeps the skill's own instructions
focused on authoring and building plans rather than on HTTP plumbing.

Auth:  reads the bearer token from PROJECTLAYER_API_TOKEN, falling back to
       CLAUDE_PLUGIN_OPTION_PROJECTLAYER_API_TOKEN (set by Claude Code from the
       plugin's install prompt).
Base:  the live ProjectLayer SaaS at https://projectlayer.app/api/v1 (fixed).

Usage:
  python projectlayer.py list-projects
  python projectlayer.py list-tasks [--project N] [--has-plan] [--status S[,S...]]
                                    [--test-outcome O] [--type T[,T...]] [--epic ID|KEY|none]
                                    [--limit N]
  python projectlayer.py list-epics [--project N] [--include-finished]
  python projectlayer.py get-task <id-or-task-key>
  python projectlayer.py update-status <id-or-task-key> <status>
  python projectlayer.py set-plan <id-or-task-key> [--file PATH]   # PATH or - for stdin
  python projectlayer.py get-test-steps <id-or-task-key>
  python projectlayer.py set-test-steps <id-or-task-key> [--file PATH]   # JSON; - for stdin
  python projectlayer.py list-test-runs <id-or-task-key>
  python projectlayer.py get-test-run <id-or-task-key> [RUN_NUMBER|current]
  python projectlayer.py record-result <id-or-task-key> <result-id> <pass|fail|pending> [--note TEXT]
  python projectlayer.py sign-off <id-or-task-key>
  python projectlayer.py create-task --project N --title TEXT [--description-file PATH]
                                     [--priority P] [--type T] [--assignee N]
                                     [--milestone N] [--plan-file PATH]
                                     [--epic ID|KEY] [--story-points N]
  python projectlayer.py list-intake [--project N] [--status S[,S...]] [--limit N]
  python projectlayer.py get-intake <intake-id>
  python projectlayer.py accept-intake <intake-id>
  python projectlayer.py reject-intake <intake-id>
  python projectlayer.py convert-intake <intake-id> [--title TEXT] [--description-file PATH]
                                        [--priority P] [--type T] [--assignee N] [--milestone N]
                                        [--epic ID|KEY] [--story-points N]

All commands print JSON to stdout on success. On failure they print a human-readable
error to stderr and exit non-zero, so the caller can react rather than guess. Exit
code 3 means the ProjectLayer server doesn't support the feature yet (test steps, or
accepting/rejecting/converting Intake items).

Write operations: update-status, set-plan, set-test-steps, record-result, sign-off,
create-task, accept-intake, reject-intake and convert-intake. Everything else is
read-only.
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

# The plugin targets the hosted ProjectLayer SaaS only. There is no self-hosted edition,
# so the base URL is fixed rather than configurable.
BASE_URL = "https://projectlayer.app/api/v1"

# List endpoints are paginated (default 25, max 100 per page), so every list is fetched
# page by page. PAGE_SIZE keeps requests few; MAX_PAGES is a safety net against an
# endless loop if the API ever returns malformed pagination metadata.
PAGE_SIZE = 100
MAX_PAGES = 1000

VALID_STATUSES = ("open", "in_progress", "on_hold", "testing", "done", "closed")
VALID_TEST_OUTCOMES = ("in_progress", "passed", "failed", "abandoned")
VALID_RESULTS = ("pass", "fail", "pending")
VALID_PRIORITIES = ("low", "medium", "high", "critical")
VALID_TYPES = ("epic", "user_story", "feature", "bug", "task", "investigation", "idea")
VALID_INTAKE_STATUSES = ("pending", "accepted", "rejected", "converted", "resolved")

# Limits the API enforces on a test script; checked here too so the user gets a crisp
# message instead of a raw 422.
MAX_TEST_STEPS = 50
MAX_STEP_TEXT = 2000

EXIT_UNSUPPORTED = 3


class ApiError(Exception):
    """An HTTP error from the API, with the server's own message when it sent one."""

    def __init__(self, status: int, message: str, url: str):
        super().__init__(message)
        self.status = status
        self.message = message
        self.url = url


def _fail(message: str, code: int = 1):
    """Print a clear, actionable error and exit. The skill reads stderr to explain
    what went wrong to the user instead of surfacing a raw traceback."""
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(code)


def _token() -> str:
    # Two supported sources, in priority order:
    #  1. PROJECTLAYER_API_TOKEN — an explicit env var, for CLI/CI or power users who
    #     want to override the configured value.
    #  2. CLAUDE_PLUGIN_OPTION_PROJECTLAYER_API_TOKEN — injected by Claude Code from the
    #     plugin's userConfig prompt (stored securely in the OS keychain). This is how a
    #     normal user supplies the token: they paste it once when enabling the plugin.
    # Strip whitespace: a trailing newline or space (common with copy-paste or
    # `export X=$(cat file)`) would otherwise be sent in the Authorization header and
    # rejected as a 401, which looks confusingly like a bad key. Treat whitespace-only
    # as unset so the user gets the "no token" guidance instead.
    token = (
        (os.environ.get("PROJECTLAYER_API_TOKEN") or "").strip()
        or (os.environ.get("CLAUDE_PLUGIN_OPTION_PROJECTLAYER_API_TOKEN") or "").strip()
    )
    if not token:
        _fail(
            "No ProjectLayer API token found. If you installed this as a plugin, enable it "
            "and paste your token when prompted (Claude Code stores it securely). Otherwise "
            "set it in your environment:\n"
            "    export PROJECTLAYER_API_TOKEN=pl_live_xxx"
        )
    return token


def _error_message(body: str) -> str:
    """Pull the human-readable message out of an error response. The API uses two
    envelopes — {"error": "..."} and {"error": {"code": ..., "message": ...}} — so
    accept both, and fall back to the raw body."""
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return body.strip()
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        return str(err.get("message") or err.get("code") or body).strip()
    if isinstance(err, str):
        return err.strip()
    return body.strip()


def _request(method: str, path: str, params: dict | None = None, body=None):
    """{method} {BASE_URL}{path} with bearer auth, returning parsed JSON (or None for
    an empty response). HTTP errors raise ApiError; network and parse errors exit."""
    url = f"{BASE_URL}{path}"
    if params:
        # Drop None values so optional filters simply don't appear in the query string.
        clean = {k: v for k, v in params.items() if v is not None}
        if clean:
            url = f"{url}?{urllib.parse.urlencode(clean)}"

    headers = {
        "Authorization": f"Bearer {_token()}",
        "Accept": "application/json",
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else None
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode("utf-8")
        except Exception:
            pass
        raise ApiError(e.code, _error_message(raw) or str(e.reason), url)
    except urllib.error.URLError as e:
        _fail(f"Could not reach {url}: {e.reason}. Check your network connection.")
    except json.JSONDecodeError:
        _fail(f"Response from {url} was not valid JSON.")


def _explain(e: ApiError):
    """Turn an ApiError into guidance the skill can act on, then exit."""
    if e.status == 400:
        _fail(f"400 Bad Request — the API rejected the request: {e.message}")
    if e.status == 401:
        _fail("401 Unauthorized — the API token is missing, invalid, revoked or expired.")
    if e.status == 403:
        # Usually a read-only key attempting a write ("lacks the 'write' scope").
        _fail(
            f"403 Forbidden — {e.message} Issue a write-scoped key from "
            "Settings → API Keys if this was a write."
        )
    if e.status == 404:
        _fail(f"404 Not Found — {e.url} does not exist (check the id/task key). {e.message}")
    if e.status == 409:
        # Conflicts are rule violations the user must resolve: open blockers on
        # done/closed, editing steps while in Testing, entering Testing with no steps,
        # signing off before every step has passed.
        _fail(f"409 Conflict — {e.message}")
    if e.status == 422:
        _fail(f"422 Validation failed — {e.message}")
    _fail(f"HTTP {e.status} from {e.url}: {e.message}")


def _call(method: str, path: str, params: dict | None = None, body=None):
    try:
        return _request(method, path, params=params, body=body)
    except ApiError as e:
        _explain(e)


def _get(path: str, params: dict | None = None):
    return _call("GET", path, params=params)


def _print(data):
    print(json.dumps(data, indent=2, ensure_ascii=False))


def _unwrap(resp):
    """List endpoints wrap results as {"data": [...], "meta": {...}}. Return
    (items, meta), tolerating a bare list or a legacy "tasks" key."""
    if isinstance(resp, dict):
        return (resp.get("data") or resp.get("tasks") or [], resp.get("meta") or {})
    return (resp or [], {})


def _iter_pages(path: str, params: dict | None = None):
    """Yield every item from a paginated list endpoint, page by page."""
    page = 1
    while True:
        query = dict(params or {}, per_page=PAGE_SIZE, page=page)
        items, meta = _unwrap(_get(path, query))
        yield from items

        total_pages = meta.get("total_pages")
        if total_pages is not None:
            if page >= total_pages:
                return
        elif len(items) < PAGE_SIZE:
            # No pagination metadata: a short or empty page means we've hit the end.
            return
        if page >= MAX_PAGES:
            return
        page += 1


def cmd_list_projects(_args):
    projects = list(_iter_pages("/projects"))
    _print({"data": projects, "meta": {"total": len(projects)}})


def _parse_statuses(value: str | None) -> list[str] | None:
    if not value:
        return None
    statuses = [s.strip().lower() for s in value.split(",") if s.strip()]
    bad = [s for s in statuses if s not in VALID_STATUSES]
    if bad:
        _fail(
            f"'{', '.join(bad)}' is not a valid status. "
            f"Use one or more of: {', '.join(VALID_STATUSES)}."
        )
    return statuses


def cmd_list_tasks(args):
    statuses = _parse_statuses(args.status)
    outcome = args.test_outcome.lower() if args.test_outcome else None
    if outcome and outcome not in VALID_TEST_OUTCOMES:
        _fail(f"'{args.test_outcome}' is not a valid test outcome. "
              f"Use one of: {', '.join(VALID_TEST_OUTCOMES)}.")

    types = None
    if args.type:
        types = [t.strip().lower() for t in args.type.split(",") if t.strip()]
        bad = [t for t in types if t not in VALID_TYPES]
        if bad:
            _fail(f"'{', '.join(bad)}' is not a valid type. Use one or more of: {', '.join(VALID_TYPES)}.")
    epic_param = None
    if args.epic:
        epic_param = "0" if args.epic.lower() == "none" else _resolve_task_id(args.epic)

    params = {
        "project_id": args.project,
        "type": ",".join(types) if types else None,
        "epic_id": epic_param,
        # The API expects the literal string "true" for the boolean filter.
        "has_plan": "true" if args.has_plan else None,
        # Only a single status is sent to the server: not every API version accepts a
        # comma-separated list, and older ones ignore `status` entirely.
        "status": statuses[0] if statuses and len(statuses) == 1 else None,
    }
    # So the status filter is always applied here as well. `last_test_outcome` has no
    # server-side filter at all.
    tasks = []
    for t in _iter_pages("/tasks", params):
        if statuses and t.get("status") not in statuses:
            continue
        if outcome and t.get("last_test_outcome") != outcome:
            continue
        # An older server ignores the type/epic filters rather than rejecting them, so
        # apply them here too — and refuse to guess about epics it doesn't know about.
        if types and t.get("type") not in types:
            continue
        if epic_param is not None:
            if "epic" not in t:
                _fail("This ProjectLayer server doesn't support epics yet.", code=EXIT_UNSUPPORTED)
        tasks.append(t)
        if args.limit and len(tasks) >= args.limit:
            break
    _print({"data": tasks, "meta": {"total": len(tasks)}})


def cmd_list_epics(args):
    """Epics with their progress. The list endpoint has no rollup, so each epic's detail
    is fetched for `epic_progress` — there are normally only a handful."""
    epics = []
    for t in _iter_pages("/tasks", {"project_id": args.project, "type": "epic"}):
        if t.get("type") != "epic":
            continue
        if not args.include_finished and t.get("status") in ("done", "closed"):
            continue
        detail = _get(f"/tasks/{t['id']}")
        if isinstance(detail, dict) and "epic_progress" not in detail:
            _fail("This ProjectLayer server doesn't support epics yet.", code=EXIT_UNSUPPORTED)
        epics.append({
            "id": t["id"], "task_key": t.get("task_key"), "title": t.get("title"),
            "project_id": t.get("project_id"), "status": t.get("status"),
            "epic_progress": (detail or {}).get("epic_progress"),
        })
    _print({"data": epics, "meta": {"total": len(epics)}})


def _resolve_task_id(ident: str) -> str:
    """Accept either a numeric id (42) or a task key (e.g. API-42) and return
    the numeric id as a string.

    Task endpoints are keyed by numeric id and the API offers no by-key lookup, so a key
    is resolved by paging through the task list until it matches. Prefixes are defined
    per project, so the key's prefix is first matched against the projects list to
    search only that project; if no active project has the prefix (e.g. it's archived),
    every task is searched. Keys are matched case-insensitively."""
    if ident.isdigit():
        return ident

    target = ident.lower()
    match = re.match(r"^(.+)-\d+$", ident)
    prefix = match.group(1).lower() if match else None

    project_ids = []
    if prefix:
        project_ids = [
            p["id"] for p in _iter_pages("/projects")
            if str(p.get("prefix", "")).lower() == prefix
        ]

    scopes = [{"project_id": pid} for pid in project_ids] or [None]
    for params in scopes:
        for t in _iter_pages("/tasks", params):
            if str(t.get("task_key", "")).lower() == target:
                return str(t["id"])

    where = "that project's tasks" if project_ids else "all tasks"
    _fail(
        f"No task with key '{ident}' found after searching {where}. Check the key is "
        "correct, or pass the numeric task id instead."
    )


def cmd_get_task(args):
    _print(_get(f"/tasks/{_resolve_task_id(args.identifier)}"))


def cmd_update_status(args):
    status = args.status.lower()
    # Validate before calling so the user gets a crisp message instead of a raw 400.
    if status not in VALID_STATUSES:
        _fail(
            f"'{args.status}' is not a valid status. "
            f"Use one of: {', '.join(VALID_STATUSES)}."
        )
    task_id = _resolve_task_id(args.identifier)
    # PATCH is the documented verb; the API also accepts POST for clients that can't
    # send PATCH, but urllib handles PATCH fine so we use it directly.
    _print(_call("PATCH", f"/tasks/{task_id}/status", body={"status": status}))


def _read_input(path: str | None, what: str) -> str:
    # Multi-line input is read from a file (or stdin) rather than an argv string — that
    # avoids shell-quoting pitfalls with newlines and quotes.
    if path in (None, "-"):
        return sys.stdin.read()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError as e:
        _fail(f"Could not read {what} file '{path}': {e}")


def cmd_set_plan(args):
    plan = _read_input(args.file, "plan").strip()
    if not plan:
        _fail("Refusing to write an empty plan. Provide plan text via --file or stdin.")

    task_id = _resolve_task_id(args.identifier)
    # Writes the generated plan (stored as Markdown) into the task's claude_plan field via
    # the partial-update endpoint. Only claude_plan is sent, so nothing else is touched.
    result = _call("PATCH", f"/tasks/{task_id}", body={"claude_plan": plan})

    # The endpoint returns the updated task, so verify the plan actually landed rather than
    # trusting the status code — if a build of the API ever ignored the field it would
    # return 200 with claude_plan unchanged, which shouldn't look like success.
    returned = (result.get("claude_plan") if isinstance(result, dict) else None) or ""
    if not returned.strip():
        _fail(
            "The API accepted the request but claude_plan is empty in the response — the "
            "plan was not stored. The plan text was NOT lost; it's the content you passed "
            "in. Check that the token has the 'write' scope and try again.",
            code=2,
        )
    # Compare tolerantly: the server may normalise line endings / trailing whitespace when
    # it stores the Markdown, and that's fine — only flag a genuine mismatch for review.
    def _norm(s: str) -> str:
        return "\n".join(line.rstrip() for line in s.replace("\r\n", "\n").split("\n")).strip()

    if _norm(returned) != _norm(plan):
        print(
            "NOTE: the stored plan differs from what was submitted (the server likely "
            "normalised the Markdown). Verify the task's plan looks right.",
            file=sys.stderr,
        )
    _print(result)


def _test_call(task_id: str, method: str, path: str, body=None):
    """Call a test-steps endpoint. A 404 there is ambiguous — the task may not exist,
    or the server may predate test steps — so on 404 check the task itself: if it
    exists but its detail has no `test_steps` field, the feature isn't deployed."""
    try:
        return _request(method, path, body=body)
    except ApiError as e:
        if e.status == 404:
            task = _get(f"/tasks/{task_id}")
            if isinstance(task, dict) and "test_steps" not in task:
                _fail(
                    "This ProjectLayer server doesn't support test steps yet. Fall back to "
                    "marking the task done once it's built and verified.",
                    code=EXIT_UNSUPPORTED,
                )
        _explain(e)


def _load_steps(path: str | None) -> list[dict]:
    raw = _read_input(path, "test steps")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        _fail(f"Test steps must be JSON: {e}")
    # Accept either the bare list or the API's own {"steps": [...]} body.
    steps = data.get("steps") if isinstance(data, dict) else data
    if not isinstance(steps, list) or not steps:
        _fail('Provide a non-empty JSON list of {"instruction", "expected_result"} objects.')
    if len(steps) > MAX_TEST_STEPS:
        _fail(f"A task can have at most {MAX_TEST_STEPS} test steps (got {len(steps)}).")

    clean = []
    for n, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            _fail(f"Step {n} must be an object with instruction and expected_result.")
        fields = {}
        for field in ("instruction", "expected_result"):
            value = str(step.get(field) or "").strip()
            if not value:
                _fail(f"Step {n} is missing {field}.")
            if len(value) > MAX_STEP_TEXT:
                _fail(f"Step {n} {field} is over {MAX_STEP_TEXT} characters.")
            fields[field] = value
        clean.append(fields)
    return clean


def cmd_get_test_steps(args):
    task_id = _resolve_task_id(args.identifier)
    _print(_test_call(task_id, "GET", f"/tasks/{task_id}/test-steps"))


def cmd_set_test_steps(args):
    steps = _load_steps(args.file)
    task_id = _resolve_task_id(args.identifier)
    # PUT replaces the whole script. The API refuses (409) while the task is in Testing,
    # so the script can't change under a tester.
    _print(_test_call(task_id, "PUT", f"/tasks/{task_id}/test-steps", body={"steps": steps}))


def cmd_list_test_runs(args):
    task_id = _resolve_task_id(args.identifier)
    _print(_test_call(task_id, "GET", f"/tasks/{task_id}/test-runs"))


def cmd_get_test_run(args):
    run = str(args.run).lower()
    if run != "current" and not run.isdigit():
        _fail(f"'{args.run}' is not a run number. Use a number or 'current'.")
    task_id = _resolve_task_id(args.identifier)
    _print(_test_call(task_id, "GET", f"/tasks/{task_id}/test-runs/{run}"))


def cmd_record_result(args):
    result = args.result.lower()
    if result not in VALID_RESULTS:
        _fail(f"'{args.result}' is not a valid result. Use one of: {', '.join(VALID_RESULTS)}.")
    note = (args.note or "").strip() or None
    if result == "fail" and not note:
        _fail("A failed step needs a --note describing what happened instead.")
    if not str(args.result_id).isdigit():
        _fail(f"'{args.result_id}' is not a result id (see get-test-run ... current).")

    task_id = _resolve_task_id(args.identifier)
    body = {"result": result}
    if note:
        body["note"] = note
    _print(_test_call(
        task_id, "PATCH", f"/tasks/{task_id}/test-runs/current/results/{args.result_id}", body=body
    ))


def cmd_sign_off(args):
    task_id = _resolve_task_id(args.identifier)
    _print(_test_call(task_id, "POST", f"/tasks/{task_id}/test-runs/current/sign-off"))


def _choice(value: str | None, allowed: tuple, what: str) -> str | None:
    if value is None:
        return None
    v = value.lower()
    if v not in allowed:
        _fail(f"'{value}' is not a valid {what}. Use one of: {', '.join(allowed)}.")
    return v


def _points(value: int) -> int:
    if value < 0 or value > 10000:
        _fail("--story-points must be between 0 and 10000.")
    return value


def _check_epic_applied(result, wanted) -> None:
    """A server without epics ignores `epic_id`; its responses then have no `epic` key, so
    a request that asked for an epic must not look like it worked."""
    if wanted and isinstance(result, dict) and "epic" not in result:
        _fail(
            "This ProjectLayer server doesn't support epics yet, so the epic was not set. "
            "The task itself was created.",
            code=EXIT_UNSUPPORTED,
        )


def cmd_create_task(args):
    title = (args.title or "").strip()
    if not title:
        _fail("A non-empty --title is required.")
    if len(title) > 500:
        _fail("The title is too long; keep it to 500 characters.")

    body = {"project_id": args.project, "title": title}
    if args.description_file:
        description = _read_input(args.description_file, "description").strip()
        if description:
            body["description"] = description
    if args.plan_file:
        plan = _read_input(args.plan_file, "plan").strip()
        if plan:
            body["claude_plan"] = plan
    priority = _choice(args.priority, VALID_PRIORITIES, "priority")
    task_type = _choice(args.type, VALID_TYPES, "type")
    if priority:
        body["priority"] = priority
    if task_type:
        body["type"] = task_type
    if args.assignee:
        body["assignee_id"] = args.assignee
    if args.milestone:
        body["milestone_id"] = args.milestone
    if args.story_points is not None:
        body["story_points"] = _points(args.story_points)
    if args.epic:
        body["epic_id"] = int(_resolve_task_id(args.epic))

    # Creates a new task (a "ticket"). Reported by the API key's user.
    result = _call("POST", "/tasks", body=body)
    _check_epic_applied(result, args.epic)
    _print(result)


def cmd_list_intake(args):
    statuses = None
    if args.status:
        statuses = [s.strip().lower() for s in args.status.split(",") if s.strip()]
        bad = [s for s in statuses if s not in VALID_INTAKE_STATUSES]
        if bad:
            _fail(f"'{', '.join(bad)}' is not a valid Intake status. "
                  f"Use one or more of: {', '.join(VALID_INTAKE_STATUSES)}.")

    params = {
        "project_id": args.project,
        # Only a single status is sent to the server; the filter is re-applied below so
        # several comma-separated statuses work too.
        "status": statuses[0] if statuses and len(statuses) == 1 else None,
    }
    items = []
    for item in _iter_pages("/backlog", params):
        if statuses and item.get("status") not in statuses:
            continue
        items.append(item)
        if args.limit and len(items) >= args.limit:
            break
    # List rows omit the description; use get-intake for the full text.
    _print({"data": items, "meta": {"total": len(items)}})


def _intake_call(intake_id: str, method: str, path: str, body=None):
    """Call an Intake endpoint. A 404 is ambiguous — the item may not exist, or the
    server may predate Intake processing — so on 404 fetch the item: if it exists but
    has no `description` field, the accept/reject/convert endpoints aren't deployed."""
    try:
        return _request(method, path, body=body)
    except ApiError as e:
        if e.status == 404 and method != "GET":
            try:
                item = _request("GET", f"/backlog/{intake_id}")
            except ApiError:
                item = None
            if isinstance(item, dict) and "description" not in item:
                _fail(
                    "This ProjectLayer server doesn't support processing Intake items "
                    "through the API yet. Accept, reject or convert the item in the "
                    "portal instead.",
                    code=EXIT_UNSUPPORTED,
                )
        _explain(e)


def _intake_id(value: str) -> str:
    if not value.isdigit():
        _fail(f"'{value}' is not an Intake item id. Use the numeric id from list-intake.")
    return value


def cmd_get_intake(args):
    intake_id = _intake_id(args.identifier)
    _print(_intake_call(intake_id, "GET", f"/backlog/{intake_id}"))


def cmd_accept_intake(args):
    intake_id = _intake_id(args.identifier)
    _print(_intake_call(intake_id, "POST", f"/backlog/{intake_id}/accept"))


def cmd_reject_intake(args):
    intake_id = _intake_id(args.identifier)
    _print(_intake_call(intake_id, "POST", f"/backlog/{intake_id}/reject"))


def cmd_convert_intake(args):
    intake_id = _intake_id(args.identifier)
    body = {}
    if args.title is not None:
        title = args.title.strip()
        if not title:
            _fail("--title cannot be empty.")
        body["title"] = title
    if args.description_file:
        body["description"] = _read_input(args.description_file, "description").strip()
    priority = _choice(args.priority, VALID_PRIORITIES, "priority")
    task_type = _choice(args.type, VALID_TYPES, "type")
    if priority:
        body["priority"] = priority
    if task_type:
        body["type"] = task_type
    if args.assignee:
        body["assignee_id"] = args.assignee
    if args.milestone:
        body["milestone_id"] = args.milestone
    if args.story_points is not None:
        body["story_points"] = _points(args.story_points)
    if args.epic:
        body["epic_id"] = int(_resolve_task_id(args.epic))

    # Creates the task and marks the item converted in one call; converting an item that
    # was already converted returns 409 with its existing task, so a retry is safe.
    result = _intake_call(intake_id, "POST", f"/backlog/{intake_id}/convert", body=body)
    if args.epic and isinstance(result, dict) and isinstance(result.get("task"), dict):
        # The convert response doesn't carry the epic, so confirm it on the new task.
        _check_epic_applied(_get(f"/tasks/{result['task']['id']}"), args.epic)
    _print(result)


def main():
    parser = argparse.ArgumentParser(description="ProjectLayer API client for the projectlayer-build skill.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list-projects", help="List all active projects").set_defaults(
        func=cmd_list_projects
    )

    lt = sub.add_parser("list-tasks", help="List tasks (optionally filtered), across all pages")
    lt.add_argument("--project", type=int, help="Filter by project_id")
    lt.add_argument("--has-plan", action="store_true", help="Only tasks that have a plan")
    lt.add_argument("--status", help=f"One or more comma-separated statuses ({', '.join(VALID_STATUSES)})")
    lt.add_argument("--test-outcome", help=f"Outcome of the latest test round ({', '.join(VALID_TEST_OUTCOMES)})")
    lt.add_argument("--limit", type=int, help="Stop after this many matching tasks")
    lt.add_argument("--type", help=f"One or more comma-separated types ({', '.join(VALID_TYPES)})")
    lt.add_argument("--epic", help="Only items in this epic (id or task key), or 'none' for items in no epic")
    lt.set_defaults(func=cmd_list_tasks)

    le = sub.add_parser("list-epics", help="List epics with their progress (items and points done/total)")
    le.add_argument("--project", type=int, help="Filter by project_id")
    le.add_argument("--include-finished", action="store_true", help="Also list Done/Closed epics")
    le.set_defaults(func=cmd_list_epics)

    gt = sub.add_parser("get-task", help="Get one task (incl. claude_plan) by id or key")
    gt.add_argument("identifier", help="Numeric task id (42) or task key (API-42)")
    gt.set_defaults(func=cmd_get_task)

    us = sub.add_parser("update-status", help="Update a task's status")
    us.add_argument("identifier", help="Numeric task id (42) or task key (API-42)")
    us.add_argument("status", help=", ".join(VALID_STATUSES))
    us.set_defaults(func=cmd_update_status)

    sp = sub.add_parser("set-plan", help="Write a build plan into a task's claude_plan field")
    sp.add_argument("identifier", help="Numeric task id (42) or task key (API-42)")
    sp.add_argument(
        "--file",
        help="Path to a file containing the plan text; use '-' or omit to read from stdin",
    )
    sp.set_defaults(func=cmd_set_plan)

    gs = sub.add_parser("get-test-steps", help="Get a task's current test script")
    gs.add_argument("identifier", help="Numeric task id (42) or task key (API-42)")
    gs.set_defaults(func=cmd_get_test_steps)

    ss = sub.add_parser("set-test-steps", help="Replace a task's test script")
    ss.add_argument("identifier", help="Numeric task id (42) or task key (API-42)")
    ss.add_argument(
        "--file",
        help='JSON list of {"instruction", "expected_result"}; use \'-\' or omit for stdin',
    )
    ss.set_defaults(func=cmd_set_test_steps)

    lr = sub.add_parser("list-test-runs", help="List a task's testing rounds, newest first")
    lr.add_argument("identifier", help="Numeric task id (42) or task key (API-42)")
    lr.set_defaults(func=cmd_list_test_runs)

    gr = sub.add_parser("get-test-run", help="Get one testing round with its results")
    gr.add_argument("identifier", help="Numeric task id (42) or task key (API-42)")
    gr.add_argument("run", nargs="?", default="current", help="Run number, or 'current' (default)")
    gr.set_defaults(func=cmd_get_test_run)

    rr = sub.add_parser("record-result", help="Record one step's result in the open round")
    rr.add_argument("identifier", help="Numeric task id (42) or task key (API-42)")
    rr.add_argument("result_id", help="The result's id, from get-test-run ... current")
    rr.add_argument("result", help="pass, fail, or pending")
    rr.add_argument("--note", help="What happened instead (required for fail)")
    rr.set_defaults(func=cmd_record_result)

    so = sub.add_parser("sign-off", help="Sign off a fully passed round and mark the task done")
    so.add_argument("identifier", help="Numeric task id (42) or task key (API-42)")
    so.set_defaults(func=cmd_sign_off)

    ct = sub.add_parser("create-task", help="Raise a new task (ticket) in a project")
    ct.add_argument("--project", type=int, required=True, help="Project id (see list-projects)")
    ct.add_argument("--title", required=True, help="Task title (max 500 characters)")
    ct.add_argument("--description-file", help="File with the description (HTML); '-' for stdin")
    ct.add_argument("--plan-file", help="File with a build plan (Markdown) to store on the task")
    ct.add_argument("--priority", help=", ".join(VALID_PRIORITIES))
    ct.add_argument("--type", help=", ".join(VALID_TYPES))
    ct.add_argument("--assignee", type=int, help="User id of an active member")
    ct.add_argument("--milestone", type=int, help="Milestone id in the same project")
    ct.add_argument("--epic", help="File the task under this epic (id or task key). Not for epics or subtasks")
    ct.add_argument("--story-points", type=int, help="Story points, 0-10000")
    ct.set_defaults(func=cmd_create_task)

    li = sub.add_parser("list-intake", help="List Intake items (optionally filtered), across all pages")
    li.add_argument("--project", type=int, help="Filter by project_id")
    li.add_argument("--status", help=f"One or more comma-separated statuses ({', '.join(VALID_INTAKE_STATUSES)})")
    li.add_argument("--limit", type=int, help="Stop after this many matching items")
    li.set_defaults(func=cmd_list_intake)

    gi = sub.add_parser("get-intake", help="Get one Intake item, including its description")
    gi.add_argument("identifier", help="Numeric Intake item id")
    gi.set_defaults(func=cmd_get_intake)

    ai = sub.add_parser("accept-intake", help="Accept a pending Intake item")
    ai.add_argument("identifier", help="Numeric Intake item id")
    ai.set_defaults(func=cmd_accept_intake)

    ri = sub.add_parser("reject-intake", help="Reject a pending Intake item")
    ri.add_argument("identifier", help="Numeric Intake item id")
    ri.set_defaults(func=cmd_reject_intake)

    ci = sub.add_parser("convert-intake", help="Convert an Intake item into a task")
    ci.add_argument("identifier", help="Numeric Intake item id")
    ci.add_argument("--title", help="Replace the title")
    ci.add_argument("--description-file", help="File with a replacement description (HTML); '-' for stdin")
    ci.add_argument("--priority", help=", ".join(VALID_PRIORITIES))
    ci.add_argument("--type", help=f"{', '.join(VALID_TYPES)} (default: bug for Sentry items, else task)")
    ci.add_argument("--assignee", type=int, help="User id of an active member")
    ci.add_argument("--milestone", type=int, help="Milestone id in the item's project")
    ci.add_argument("--epic", help="File the new task under this epic (id or task key)")
    ci.add_argument("--story-points", type=int, help="Story points, 0-10000")
    ci.set_defaults(func=cmd_convert_intake)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
