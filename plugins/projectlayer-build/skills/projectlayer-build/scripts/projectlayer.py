#!/usr/bin/env python3
"""
Thin client for the ProjectLayer REST API.

Why this exists: every invocation of the projectlayer-build skill needs the same
handful of calls with the same auth header and the same error handling. Doing that
once here — with clear, actionable error messages — keeps the skill's own instructions
focused on authoring and building plans rather than on HTTP plumbing.

Auth:  reads the bearer token from the PROJECTLAYER_API_TOKEN environment variable.
Base:  the live ProjectLayer SaaS at https://projectlayer.app/api/v1 (fixed).

Usage:
  python projectlayer.py list-projects
  python projectlayer.py list-tasks [--project N] [--has-plan] [--status STATUS]
  python projectlayer.py get-task <id-or-task-key>
  python projectlayer.py update-status <id-or-task-key> <open|in_progress|done|closed>
  python projectlayer.py set-plan <id-or-task-key> [--file PATH]   # PATH or - for stdin

All commands print JSON to stdout on success. On failure they print a human-readable
error to stderr and exit non-zero, so the caller can react rather than guess.

Write operations: update-status and set-plan. Everything else is read-only.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

# The plugin targets the hosted ProjectLayer SaaS only. There is no self-hosted edition,
# so the base URL is fixed rather than configurable.
BASE_URL = "https://projectlayer.app/api/v1"

# Task keys are resolved by paging through the task list (the API has no by-key lookup).
# PAGE_SIZE keeps requests few; MAX_PAGES is a safety net against an endless loop if the
# API ever returns malformed pagination metadata.
PAGE_SIZE = 100
MAX_PAGES = 1000


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


def _request(method: str, path: str, params: dict | None = None, body: dict | None = None):
    """{method} {BASE_URL}{path} with bearer auth, returning parsed JSON.

    Errors are translated into guidance the skill can act on: a 401 means the token
    is bad, a 404 means the task/project doesn't exist, a 400 means the payload was
    rejected (e.g. an unknown status value), etc."""
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
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8")
        except Exception:
            pass
        if e.code == 400:
            _fail(f"400 Bad Request — the API rejected the payload: {body or e.reason}")
        if e.code == 401:
            _fail("401 Unauthorized — the API token is missing, invalid, or expired.")
        if e.code == 403:
            _fail("403 Forbidden — the token lacks access to this workspace/resource.")
        if e.code == 404:
            _fail(f"404 Not Found — {url} does not exist (check the id/task key).")
        _fail(f"HTTP {e.code} from {url}: {body or e.reason}")
    except urllib.error.URLError as e:
        _fail(f"Could not reach {url}: {e.reason}. Check your network connection.")
    except json.JSONDecodeError:
        _fail(f"Response from {url} was not valid JSON.")


def _get(path: str, params: dict | None = None):
    return _request("GET", path, params=params)


def _print(data):
    print(json.dumps(data, indent=2, ensure_ascii=False))


def cmd_list_projects(_args):
    _print(_get("/projects"))


def cmd_list_tasks(args):
    params = {
        "project_id": args.project,
        # The API expects the literal string "true" for the boolean filter.
        "has_plan": "true" if args.has_plan else None,
        "status": args.status,
    }
    _print(_get("/tasks", params))


def _unwrap(resp):
    """The list endpoint wraps results as {"data": [...], "meta": {...}}. Return
    (items, meta), tolerating a bare list or a legacy "tasks" key."""
    if isinstance(resp, dict):
        return (resp.get("data") or resp.get("tasks") or [], resp.get("meta") or {})
    return (resp or [], {})


def _resolve_task_id(ident: str) -> str:
    """Accept either a numeric id (42) or a task key (e.g. API-42, RSPH-14) and return
    the numeric id as a string.

    Task endpoints are keyed by numeric id and the API offers no by-key lookup or filter,
    so a key is resolved by paging through the task list until it matches. Task-key
    prefixes are user-defined per project, so nothing here assumes a particular prefix —
    the given key is matched case-insensitively against each task's `task_key`."""
    if ident.isdigit():
        return ident

    target = ident.lower()
    page = 1
    pages_scanned = 0
    while True:
        items, meta = _unwrap(_get("/tasks", {"per_page": PAGE_SIZE, "page": page}))
        for t in items:
            if str(t.get("task_key", "")).lower() == target:
                return str(t["id"])
        pages_scanned += 1

        total_pages = meta.get("total_pages")
        if total_pages is not None:
            if page >= total_pages:
                break
        elif len(items) < PAGE_SIZE:
            # No pagination metadata: a short or empty page means we've hit the end.
            break
        if pages_scanned >= MAX_PAGES:
            break
        page += 1

    _fail(
        f"No task with key '{ident}' found after scanning {pages_scanned} page(s) of "
        "tasks. Check the key is correct, or pass the numeric task id instead."
    )


def cmd_get_task(args):
    _print(_get(f"/tasks/{_resolve_task_id(args.identifier)}"))


VALID_STATUSES = ("open", "in_progress", "done", "closed")


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
    _print(_request("PATCH", f"/tasks/{task_id}/status", body={"status": status}))


def cmd_set_plan(args):
    # The plan text is usually multi-line, so we read it from a file (or stdin) rather
    # than an argv string — that avoids shell-quoting pitfalls with newlines and quotes.
    if args.file in (None, "-"):
        plan = sys.stdin.read()
    else:
        try:
            with open(args.file, "r", encoding="utf-8") as fh:
                plan = fh.read()
        except OSError as e:
            _fail(f"Could not read plan file '{args.file}': {e}")
    plan = plan.strip()
    if not plan:
        _fail("Refusing to write an empty plan. Provide plan text via --file or stdin.")

    task_id = _resolve_task_id(args.identifier)
    # Writes the generated plan (stored as Markdown) into the task's claude_plan field via
    # the partial-update endpoint. Only claude_plan is sent, so nothing else is touched.
    result = _request("PATCH", f"/tasks/{task_id}", body={"claude_plan": plan})

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


def main():
    parser = argparse.ArgumentParser(description="Read-only ProjectLayer API client.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list-projects", help="List all active projects").set_defaults(
        func=cmd_list_projects
    )

    lt = sub.add_parser("list-tasks", help="List tasks (optionally filtered)")
    lt.add_argument("--project", type=int, help="Filter by project_id")
    lt.add_argument("--has-plan", action="store_true", help="Only tasks that have a plan")
    lt.add_argument("--status", help="Filter by status (open/in_progress/done/closed)")
    lt.set_defaults(func=cmd_list_tasks)

    gt = sub.add_parser("get-task", help="Get one task (incl. claude_plan) by id or key")
    gt.add_argument("identifier", help="Numeric task id (42) or task key (API-42)")
    gt.set_defaults(func=cmd_get_task)

    us = sub.add_parser("update-status", help="Update a task's status")
    us.add_argument("identifier", help="Numeric task id (42) or task key (API-42)")
    us.add_argument("status", help="open, in_progress, done, or closed")
    us.set_defaults(func=cmd_update_status)

    sp = sub.add_parser("set-plan", help="Write a build plan into a task's claude_plan field")
    sp.add_argument("identifier", help="Numeric task id (42) or task key (API-42)")
    sp.add_argument(
        "--file",
        help="Path to a file containing the plan text; use '-' or omit to read from stdin",
    )
    sp.set_defaults(func=cmd_set_plan)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
