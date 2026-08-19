"""Headless Claude reviewer. Spawns `claude -p` in the project dir so that
`review_rules.md` is the source of truth (no repo-specific CLAUDE.md leaks in).

Writes findings directly to the DB — does NOT go through the HTTP API
(would deadlock the event loop on a self-call).
"""
import asyncio
import json
import re
from pathlib import Path

from . import config, db, gh

PROJECT_DIR = Path(__file__).parent.parent  # pr-watcher-ui/
PROMPT_PATH = Path(__file__).parent / "reviewer_prompt.md"
ADDRESS_CHECK_PROMPT_PATH = Path(__file__).parent / "address_check_prompt.md"
RULES_PATH = PROJECT_DIR / "review_rules.md"

# How long to let Claude run per review (seconds)
REVIEW_TIMEOUT = 600

# Cap on concurrent `claude -p` subprocesses. Lets the user review several
# heads in parallel without spawning N subprocesses if the follow-up watcher
# fires re-reviews on every tracked PR at once.
_MAX_PARALLEL_REVIEWS = 4
_REVIEW_SEM = asyncio.Semaphore(_MAX_PARALLEL_REVIEWS)

# Active review subprocesses keyed by PR number, so dismiss/approve can kill
# an in-flight claude -p when the user wipes the row mid-review. Without this,
# the subprocess runs to completion, keeps holding a semaphore slot, and
# jams every queued PR behind it until the UI restarts.
_ACTIVE_PROCS: dict[int, asyncio.subprocess.Process] = {}


def kill_review(pr_number: int) -> bool:
    """Kill an in-flight review subprocess for `pr_number`, if any.

    Returns True if a process was killed. Safe to call when no review is
    running. Called from dismiss/approve to release the semaphore slot.
    """
    proc = _ACTIVE_PROCS.get(pr_number)
    if proc is None or proc.returncode is not None:
        return False
    try:
        proc.kill()
    except ProcessLookupError:
        return False
    db.log_action(pr_number, "review_killed", "user dismissed/approved mid-review")
    return True

FINDINGS_RE = re.compile(r"<FINDINGS>\s*(\[.*?\])\s*</FINDINGS>", re.DOTALL)
VERDICT_RE = re.compile(r"<VERDICT>\s*(\{.*?\})\s*</VERDICT>", re.DOTALL)


def _build_prompt(pr_number: int) -> str:
    template = PROMPT_PATH.read_text()
    rules = RULES_PATH.read_text()
    context = _build_previous_review_context(pr_number)
    return (
        template
        .replace("{PR_NUMBER}", str(pr_number))
        .replace("{REPO}", config.repo())
        .replace("{SELF_LOGIN}", config.self_login())
        .replace("{RULES}", rules)
        .replace("{PREVIOUS_REVIEW_CONTEXT}", context)
    )


def _build_previous_review_context(pr_number: int) -> str:
    """Assemble the 'previous review' block.

    For each POSTED finding on this PR, include the comment we posted plus any
    replies from other users. This lets the reviewer decide whether the author
    has resolved the finding (via commit fix OR valid reply).
    """
    from . import watchers  # avoid circular import at module load
    with db.conn() as c:
        posted = c.execute(
            """SELECT id, severity, file, line, title, suggestion_body,
                      github_comment_id
               FROM findings
               WHERE pr_number=? AND status='posted'
               ORDER BY id""",
            (pr_number,),
        ).fetchall()
    if not posted:
        return "(No previous findings posted — this is a first-time review.)"

    try:
        all_comments = gh.fetch_all_review_comments(pr_number)
    except Exception as e:
        db.log_action(pr_number, "fetch_comments_failed", str(e))
        all_comments = []

    # Build: comment_id -> list of replies (from anyone other than us)
    replies_by_parent = {}
    for cm in all_comments:
        parent = cm.get("in_reply_to_id")
        if parent and cm.get("user", {}).get("login") != watchers.SELF_LOGIN:
            replies_by_parent.setdefault(parent, []).append(cm)

    sections = []
    for p in posted:
        cid = p["github_comment_id"]
        replies = replies_by_parent.get(cid, [])
        reply_text = (
            "\n".join(
                f"- @{r['user']['login']} at {r['updated_at']}:\n  "
                + r.get("body", "").replace("\n", "\n  ")
                for r in replies
            )
            if replies else "(no replies)"
        )
        sections.append(
            f"## Posted finding {p['id']}: {p['title']}\n"
            f"- File: `{p['file']}:{p['line']}`\n"
            f"- Severity: {p['severity']}\n"
            f"- Our comment body:\n\n{p['suggestion_body']}\n\n"
            f"- Author/other replies:\n{reply_text}\n"
        )
    return "\n---\n\n".join(sections)


async def review_pr(pr_number: int) -> dict:
    """Run Claude review and write findings directly to DB.

    Reviews are user-triggered only (via /api/prs/{number}/review).
    `_REVIEW_SEM` caps total concurrent subprocesses at
    `_MAX_PARALLEL_REVIEWS` so multiple heads can be reviewed in parallel
    without spawning unboundedly when the follow-up watcher fires.
    """
    return await _do_review(pr_number)


async def _do_review(pr_number: int) -> dict:
    async with _REVIEW_SEM:
        return await _do_review_locked(pr_number)


async def _do_review_locked(pr_number: int) -> dict:
    # Bail early if the DB row was deleted while this task was waiting on the
    # lock (e.g. a concurrent auto-approve already handled it, or the user
    # dismissed the PR). Without this guard, stacked review tasks all run to
    # completion and each posts a duplicate approval on the now-gone row.
    with db.conn() as c:
        row = c.execute(
            "SELECT 1 FROM prs WHERE number=?", (pr_number,)
        ).fetchone()
    if row is None:
        return {"ok": True, "skipped": "row_deleted"}

    # Bail early if the PR is no longer open — merged or closed externally.
    # Don't spawn claude, don't promote to HEAD; just drop the row.
    try:
        state = gh.pr_state(pr_number)
    except Exception:
        state = None
    if state in ("MERGED", "CLOSED"):
        with db.conn() as c:
            c.execute("DELETE FROM prs WHERE number=?", (pr_number,))
        db.log_action(pr_number, "auto_removed", f"pr state: {state.lower()}")
        return {"ok": True, "removed": True, "state": state}

    # Bail if the PR is a draft. The new-PR watcher filters drafts at detection
    # time, but a PR can flip back to draft, or be added manually via /api/prs
    # without the filter — so re-check right before we'd spend a review on it.
    try:
        if gh.is_draft(pr_number):
            with db.conn() as c:
                c.execute("DELETE FROM prs WHERE number=?", (pr_number,))
            db.log_action(pr_number, "auto_removed", "pr is draft")
            return {"ok": True, "removed": True, "reason": "draft"}
    except Exception:
        pass

    # Bail if the PR already has any approval. Never re-approve an
    # already-approved PR — belt-and-braces against duplicate approvals when
    # multiple review tasks get stacked for the same PR.
    try:
        if gh.approvals_count(pr_number) > 0:
            with db.conn() as c:
                c.execute("DELETE FROM prs WHERE number=?", (pr_number,))
            db.log_action(pr_number, "auto_removed", "already approved")
            return {"ok": True, "removed": True, "reason": "already_approved"}
    except Exception:
        pass

    prompt = _build_prompt(pr_number)
    db.log_action(pr_number, "review_started", "")
    with db.conn() as c:
        c.execute(
            "UPDATE prs SET status='reviewing', updated_at=datetime('now') WHERE number=?",
            (pr_number,),
        )

    proc = await asyncio.create_subprocess_exec(
        "claude", "-p", prompt,
        "--dangerously-skip-permissions",
        cwd=str(PROJECT_DIR),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _ACTIVE_PROCS[pr_number] = proc
    try:
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=REVIEW_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            db.log_action(pr_number, "review_timeout", f"exceeded {REVIEW_TIMEOUT}s")
            # Leave the PR recoverable: mark it failed (surfaces in the UI with a
            # retry button) instead of leaving status='reviewing' forever, which
            # would occupy HEAD and jam the queue until the next server restart.
            with db.conn() as c:
                c.execute(
                    "UPDATE prs SET status='review_failed', updated_at=datetime('now') WHERE number=?",
                    (pr_number,),
                )
            return {"ok": False, "error": "timeout"}
    finally:
        _ACTIVE_PROCS.pop(pr_number, None)

    # If the subprocess was killed via kill_review (dismiss/approve mid-review),
    # the row was already deleted — bail before parsing output or approving.
    if proc.returncode is not None and proc.returncode < 0:
        return {"ok": True, "killed": True}

    out = stdout.decode(errors="replace")
    err = stderr.decode(errors="replace")

    if proc.returncode != 0:
        db.log_action(pr_number, "review_failed", err[:500])
        with db.conn() as c:
            c.execute("UPDATE prs SET status='review_failed' WHERE number=?", (pr_number,))
        return {"ok": False, "error": err}

    m = FINDINGS_RE.search(out)
    if not m:
        db.log_action(pr_number, "review_parse_failed", out[-500:])
        with db.conn() as c:
            c.execute("UPDATE prs SET status='review_failed' WHERE number=?", (pr_number,))
        return {"ok": False, "error": "no <FINDINGS> block in output"}

    try:
        findings = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        db.log_action(pr_number, "review_parse_failed", str(e))
        return {"ok": False, "error": f"invalid JSON: {e}"}

    # Drop findings whose `file` isn't in the PR's diff. GitHub's inline-
    # comment endpoint returns 422 "path could not be resolved" when the
    # reviewer hallucinates a file path (or guesses casing/extension). It's
    # cheaper to filter here than to let the user discover unpostable rows.
    try:
        diff_paths = gh.diff_file_paths(pr_number)
    except Exception as e:
        diff_paths = None
        db.log_action(pr_number, "diff_paths_fetch_failed", str(e))
    if diff_paths is not None:
        kept, dropped = [], []
        for f in findings:
            path = f.get("file")
            if path and path in diff_paths:
                kept.append(f)
            else:
                dropped.append(path or "(no file)")
        if dropped:
            db.log_action(
                pr_number,
                "findings_dropped_not_in_diff",
                f"{len(dropped)} dropped: " + ", ".join(dropped[:10]),
            )
        findings = kept

    # Write findings directly to DB (same process — no HTTP self-call).
    # ALL findings (any severity) go to the UI as pending for user decision.
    _insert_findings(pr_number, findings)

    # Snapshot current PR state so the follow-up watcher can detect changes
    # that happen AFTER the review (new commits or replies from the author).
    _snapshot_baseline(pr_number)

    # No auto-approve. If findings are empty, leave the PR on HEAD as
    # awaiting_user so the user can manually approve via the UI.
    if not findings:
        with db.conn() as c:
            c.execute(
                "UPDATE prs SET status='awaiting_user', updated_at=datetime('now') WHERE number=?",
                (pr_number,),
            )
        from . import watchers
        watchers.notify(f"#{pr_number}: clean review — ready to approve")

    db.log_action(pr_number, "review_done", f"{len(findings)} findings")
    return {"ok": True, "findings": len(findings)}


def _insert_findings(pr_number: int, findings: list[dict]) -> None:
    """Insert each finding as `pending`, skipping ones already posted.

    De-dup key is (file, line, title). If the reviewer re-reports an issue
    that's already been posted to GitHub, we don't insert a pending dup —
    the existing inline comment already covers it and re-posting would
    spam the PR. In that case the PR stays in `pending_author` (off HEAD);
    only genuinely new findings promote the PR to `awaiting_user`.
    """
    if not findings:
        return
    with db.conn() as c:
        posted_keys = {
            (r["file"], r["line"], r["title"])
            for r in c.execute(
                "SELECT file, line, title FROM findings "
                "WHERE pr_number=? AND status='posted'",
                (pr_number,),
            ).fetchall()
        }

    new_findings = [
        f for f in findings
        if (f.get("file"), f.get("line"), f.get("title")) not in posted_keys
    ]

    with db.conn() as c:
        for f in new_findings:
            c.execute(
                """INSERT INTO findings
                   (pr_number, severity, file, line, title, message,
                    code_snippet, blast_radius, confidence, fix, suggestion_body,
                    plain_verdict, plain_title, plain_summary, plain_impact_label,
                    plain_impact, plain_body)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    pr_number,
                    f.get("severity", "suggestion"),
                    f.get("file"),
                    f.get("line"),
                    f.get("title", "(untitled)"),
                    f.get("message", ""),
                    f.get("code_snippet"),
                    f.get("blast_radius"),
                    f.get("confidence"),
                    f.get("fix"),
                    f.get("suggestion_body"),
                    f.get("plain_verdict"),
                    f.get("plain_title"),
                    f.get("plain_summary"),
                    f.get("plain_impact_label"),
                    f.get("plain_impact"),
                    f.get("plain_body"),
                ),
            )
        if new_findings:
            c.execute(
                "UPDATE prs SET status='awaiting_user', updated_at=datetime('now') WHERE number=?",
                (pr_number,),
            )
        else:
            # Re-review only reconfirmed already-posted findings — the
            # author hasn't resolved them yet. Keep the PR as pending_author
            # so it stays off HEAD; no new notification.
            c.execute(
                "UPDATE prs SET status='pending_author', updated_at=datetime('now') WHERE number=?",
                (pr_number,),
            )
            db.log_action(
                pr_number,
                "findings_deduped",
                f"all {len(findings)} finding(s) already posted — staying pending_author",
            )

    if new_findings:
        # Only notify when the user actually has something to click on.
        from . import watchers  # avoid circular at module import
        watchers.notify(f"#{pr_number}: {len(new_findings)} finding(s) ready to review")


def _snapshot_baseline(pr_number: int):
    """Record current head SHA and latest comment timestamps from others.

    Anything newer than this after the review means the author did something —
    commit, reply inline, or comment on the PR thread.
    """
    from . import watchers  # avoid circular at import time
    try:
        sha = gh.latest_commit_sha(pr_number)
        review_at = gh.latest_review_comment_at(pr_number, watchers.SELF_LOGIN)
        issue_at = gh.latest_issue_comment_at(pr_number, watchers.SELF_LOGIN)
    except Exception as e:
        db.log_action(pr_number, "baseline_failed", str(e))
        return
    with db.conn() as c:
        c.execute(
            """UPDATE prs SET
                 head_sha=?,
                 last_seen_commit_sha=?,
                 last_seen_review_comment_at=?,
                 last_seen_issue_comment_at=?,
                 has_new_activity=0
               WHERE number=?""",
            (sha, sha, review_at, issue_at, pr_number),
        )


def schedule_review(pr_number: int):
    """Fire-and-forget. Call from sync code (e.g. the watcher)."""
    loop = asyncio.get_event_loop()
    loop.create_task(review_pr(pr_number))


# ---- Approve-if-addressed -------------------------------------------------
# Spawn Claude with a focused prompt that ONLY classifies each posted finding
# as addressed/explained/minor/unresolved, no fresh review. If every item is
# resolved, the caller can approve. Otherwise the verdict is surfaced to the
# user and the PR is bumped back to pending_author.
def _build_address_check_prompt(pr_number: int) -> str:
    posted_block = _build_posted_findings_block(pr_number)
    template = ADDRESS_CHECK_PROMPT_PATH.read_text()
    return (
        template
        .replace("{PR_NUMBER}", str(pr_number))
        .replace("{REPO}", config.repo())
        .replace("{SELF_LOGIN}", config.self_login())
        .replace("{POSTED_FINDINGS}", posted_block)
    )


def _build_posted_findings_block(pr_number: int) -> str:
    """Build the per-finding context block for the address check.

    Source of truth is GitHub: every inline comment authored by SELF_LOGIN on
    the PR is treated as a finding to verify, regardless of whether it was
    posted via this UI or via `gh` directly. Where the local DB has matching
    rich context (severity/title), we layer it in.
    """
    from . import watchers  # avoid circular import
    try:
        all_comments = gh.fetch_all_review_comments(pr_number)
    except Exception as e:
        db.log_action(pr_number, "fetch_comments_failed", str(e))
        all_comments = []

    my_comments = [
        c for c in all_comments
        if c.get("user", {}).get("login") == watchers.SELF_LOGIN
        and not c.get("in_reply_to_id")
    ]
    if not my_comments:
        return "(No inline comments by you on this PR — nothing to verify.)"

    replies_by_parent = {}
    for cm in all_comments:
        parent = cm.get("in_reply_to_id")
        if parent and cm.get("user", {}).get("login") != watchers.SELF_LOGIN:
            replies_by_parent.setdefault(parent, []).append(cm)

    with db.conn() as c:
        db_by_gh_id = {
            r["github_comment_id"]: r
            for r in c.execute(
                """SELECT id, severity, title, github_comment_id
                   FROM findings
                   WHERE pr_number=? AND status='posted'
                     AND github_comment_id IS NOT NULL""",
                (pr_number,),
            ).fetchall()
        }

    sections = []
    for cm in my_comments:
        cid = cm["id"]
        db_row = db_by_gh_id.get(cid)
        if db_row:
            heading = f"## Finding {db_row['id']}: {db_row['title']}"
            severity_line = f"- Severity: {db_row['severity']}\n"
        else:
            heading = f"## Finding gh-{cid}: (posted directly via gh)"
            severity_line = ""
        replies = replies_by_parent.get(cid, [])
        reply_text = (
            "\n".join(
                f"- @{r['user']['login']} at {r['updated_at']}:\n  "
                + r.get("body", "").replace("\n", "\n  ")
                for r in replies
            )
            if replies else "(no replies)"
        )
        sections.append(
            f"{heading}\n"
            f"- File: `{cm.get('path')}:{cm.get('line') or cm.get('original_line')}`\n"
            f"{severity_line}"
            f"- Our comment body:\n\n{cm.get('body', '')}\n\n"
            f"- Author/other replies:\n{reply_text}\n"
        )
    return "\n---\n\n".join(sections)


async def verify_addressed(pr_number: int) -> dict:
    """Spawn Claude to classify each posted finding. Returns the parsed verdict.

    Caller is responsible for acting on the verdict (approving vs. surfacing
    unresolved items). Shares the same _REVIEW_SEM as reviews so the total
    number of Claude subprocesses is capped together.
    """
    async with _REVIEW_SEM:
        return await _do_verify_addressed_locked(pr_number)


async def _do_verify_addressed_locked(pr_number: int) -> dict:
    with db.conn() as c:
        row = c.execute(
            "SELECT 1 FROM prs WHERE number=?", (pr_number,)
        ).fetchone()
    if row is None:
        return {"ok": False, "error": "row_deleted"}

    prompt = _build_address_check_prompt(pr_number)
    db.log_action(pr_number, "address_check_started", "")
    with db.conn() as c:
        c.execute(
            "UPDATE prs SET status='reviewing', updated_at=datetime('now') WHERE number=?",
            (pr_number,),
        )

    proc = await asyncio.create_subprocess_exec(
        "claude", "-p", prompt,
        "--dangerously-skip-permissions",
        cwd=str(PROJECT_DIR),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _ACTIVE_PROCS[pr_number] = proc
    try:
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=REVIEW_TIMEOUT
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            db.log_action(pr_number, "address_check_timeout", f"exceeded {REVIEW_TIMEOUT}s")
            with db.conn() as c:
                c.execute(
                    "UPDATE prs SET status='pending_author', updated_at=datetime('now') WHERE number=?",
                    (pr_number,),
                )
            return {"ok": False, "error": "timeout"}
    finally:
        _ACTIVE_PROCS.pop(pr_number, None)

    if proc.returncode is not None and proc.returncode < 0:
        return {"ok": False, "error": "killed"}

    out = stdout.decode(errors="replace")
    err = stderr.decode(errors="replace")

    if proc.returncode != 0:
        db.log_action(pr_number, "address_check_failed", err[:500])
        with db.conn() as c:
            c.execute(
                "UPDATE prs SET status='pending_author', updated_at=datetime('now') WHERE number=?",
                (pr_number,),
            )
        return {"ok": False, "error": err}

    m = VERDICT_RE.search(out)
    if not m:
        db.log_action(pr_number, "address_check_parse_failed", out[-500:])
        with db.conn() as c:
            c.execute(
                "UPDATE prs SET status='pending_author', updated_at=datetime('now') WHERE number=?",
                (pr_number,),
            )
        return {"ok": False, "error": "no <VERDICT> block in output"}

    try:
        verdict = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        db.log_action(pr_number, "address_check_parse_failed", str(e))
        return {"ok": False, "error": f"invalid JSON: {e}"}

    return {"ok": True, "verdict": verdict}
