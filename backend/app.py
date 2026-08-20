"""FastAPI app: serves UI, API, and runs watchers in the same process."""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from . import chat, config, db, gh, merger, my_prs, reviewer, watchers

FRONTEND = Path(__file__).parent.parent / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    # Self-heal: any PR stuck in 'reviewing' when the server starts has a dead
    # subprocess (killed by reload or crash). Reset so it can be retried.
    with db.conn() as c:
        stuck = c.execute(
            "SELECT number FROM prs WHERE status='reviewing'"
        ).fetchall()
        if stuck:
            c.execute("UPDATE prs SET status='queued' WHERE status='reviewing'")
            for row in stuck:
                c.execute(
                    "INSERT INTO activity_log (pr_number, action, details) "
                    "VALUES (?, 'stuck_review_reset', 'server restart')",
                    (row["number"],),
                )
    # Reviews are user-triggered only — do NOT auto-kick queued PRs on startup.
    # Queued rows wait for the user to click Review in the UI.
    await watchers.run_forever()
    yield


app = FastAPI(lifespan=lifespan)


# ---------- Models ---------------------------------------------------------
class PRIn(BaseModel):
    number: int
    author: str
    title: str
    url: str
    head_sha: str | None = None
    jira_key: str | None = None
    jira_summary: str | None = None


class FindingIn(BaseModel):
    severity: str  # critical | important | suggestion
    title: str
    message: str
    file: str | None = None
    line: int | None = None
    code_snippet: str | None = None
    blast_radius: str | None = None
    confidence: str | None = None
    fix: str | None = None
    suggestion_body: str | None = None
    plain_verdict: str | None = None
    plain_title: str | None = None
    plain_summary: str | None = None
    plain_impact_label: str | None = None
    plain_impact: str | None = None
    plain_body: str | None = None


# ---------- Queries --------------------------------------------------------
def _get_state():
    with db.conn() as c:
        prs = db.rows_to_dicts(c.execute(
            "SELECT * FROM prs ORDER BY created_at ASC"
        ).fetchall())
        findings = db.rows_to_dicts(c.execute(
            "SELECT * FROM findings ORDER BY severity, id ASC"
        ).fetchall())
        runs = db.rows_to_dicts(c.execute(
            "SELECT * FROM watcher_runs"
        ).fetchall())
        activity = db.rows_to_dicts(c.execute(
            "SELECT * FROM activity_log ORDER BY id DESC LIMIT 20"
        ).fetchall())

    # Group findings per PR
    by_pr = {}
    for f in findings:
        by_pr.setdefault(f["pr_number"], []).append(f)

    # HEAD priority: prefer PRs that need user action (awaiting_user,
    # review_failed) before in-progress (reviewing) or waiting-for-slot
    # (queued). Inside each bucket, pick the oldest PR. This prevents a
    # re-queued PR (author pushed commit → we want a fresh re-review) from
    # showing up as HEAD with stale posted findings while another PR is
    # actually awaiting the user's decision.
    head_priority = {
        "awaiting_user": 0,
        "review_failed": 0,
        "reviewing": 1,
        "queued": 2,
    }
    head_candidates = []
    pinned = []
    awaiting_author = []
    review_pool = []  # all PRs that COULD be head (pinned, parked, or candidates)
    for p in prs:
        p["findings"] = by_pr.get(p["number"], [])
        pending_count = sum(1 for f in p["findings"] if f["status"] == "pending")
        p["pending_findings"] = pending_count
        if p["status"] == "pending_author":
            awaiting_author.append(p)
        elif p["status"] in head_priority:
            review_pool.append(p)
            if p["pinned_at"]:
                pinned.append(p)
            elif not p["parked"]:
                head_candidates.append(p)

    if pinned:
        pinned.sort(key=lambda p: p["pinned_at"], reverse=True)
        heads = pinned
    else:
        head_candidates.sort(key=lambda p: (head_priority[p["status"]], p["created_at"]))
        heads = [head_candidates[0]] if head_candidates else []

    head_numbers = {h["number"] for h in heads}
    pending_review = [p for p in review_pool if p["number"] not in head_numbers]
    pending_review.sort(key=lambda p: (head_priority[p["status"]], p["created_at"]))

    return {
        "heads": heads,
        "pending_review": pending_review,
        "awaiting_author": awaiting_author,
        "watchers": runs,
        "activity": activity,
    }


# ---------- Routes: UI -----------------------------------------------------
@app.get("/")
def root():
    return FileResponse(FRONTEND / "index.html")


# ---------- Routes: state --------------------------------------------------
@app.get("/api/state")
def state():
    return _get_state()


@app.get("/api/meta")
def meta():
    """Static config the frontend needs — chiefly the repo slug so it can build
    PR links without hardcoding any owner/name.
    """
    return {
        "repo": config.repo(),
        "self_login": config.self_login(),
        # Where the user's checkout lives, so the My PRs tab can name the
        # directory to run a suggested fix in rather than saying "your repo".
        "target_repo_dir": config.target_repo_dir(),
        # Whether the Teams send button can work, and what it should say.
        "teams_configured": bool(config.teams_webhook_url()),
        "teams_channel_label": config.teams_channel_label(),
    }


@app.get("/api/my_prs")
def my_prs_sidebar():
    """The user's own open PRs + approval status, for the follow-up sidebar.

    Kept out of /api/state so its gh latency doesn't slow the 5s state poll —
    the frontend polls this on its own slower cadence. Degrades to an empty
    list with an error string (HTTP 200) so a gh hiccup never breaks the page.
    """
    try:
        prs = gh.list_my_open_prs(watchers.SELF_LOGIN)
    except Exception as e:  # noqa: BLE001 - surface any gh failure to the UI
        return JSONResponse({"prs": [], "error": str(e)})
    # Layer in merge state so the sidebar can show a "merging…" spinner and
    # any merge failure. Lives in memory (my_prs is otherwise a stateless gh
    # fetch); see merger.py.
    for p in prs:
        p["merging"] = p["number"] in merger.MERGING
        p["merge_error"] = merger.MERGE_ERRORS.get(p["number"])
    return {"prs": prs}


@app.get("/api/my_prs/triage")
def my_prs_triage_view():
    """Own PRs bucketed, with outstanding threads and any stored triage.

    Separate from /api/my_prs: this one walks every PR's comment threads, so it
    is slow enough that the sidebar should not wait on it.
    """
    try:
        prs = my_prs.gather(config.self_login())
    except Exception as e:  # noqa: BLE001 - never blank the tab on a gh hiccup
        return JSONResponse({"prs": [], "error": str(e)})
    for p in prs:
        p["merging"] = p["number"] in merger.MERGING
        p["merge_error"] = merger.MERGE_ERRORS.get(p["number"])
    return {"prs": prs}


@app.post("/api/my_prs/{number}/triage")
async def triage_my_pr(number: int):
    result = await my_prs.analyse(number)
    if not result["ok"]:
        raise HTTPException(400, result["error"])
    return result


class ChatIn(BaseModel):
    message: str


class ReviewRequestIn(BaseModel):
    summary: str


@app.post("/api/my_prs/{number}/review_request")
async def draft_review_request(number: int):
    """Write the one-line context blurb for a review request."""
    result = await my_prs.draft_review_request(number)
    if not result["ok"]:
        raise HTTPException(400, result["error"])
    return result


@app.post("/api/my_prs/{number}/review_request/send")
async def send_review_request(number: int, payload: ReviewRequestIn):
    """Post the review request into the configured Teams channel.

    Only ever reached by a button press. The summary comes from the request
    body rather than the stored draft so the user's edits are what gets sent.
    """
    with db.conn() as c:
        row = c.execute(
            "SELECT * FROM review_requests WHERE pr_number=?", (number,)
        ).fetchone()
    if row is None:
        raise HTTPException(404, "no drafted request for this PR")
    summary = (payload.summary or "").strip() or row["summary"]
    result = my_prs.send_to_teams(number, summary, row["title"], row["url"])
    if not result["ok"]:
        raise HTTPException(500, result["error"])
    watchers.notify(f"Posted a review request for #{number}")
    return {"ok": True}


class ThreadReplyIn(BaseModel):
    body: str
    kind: str  # inline | issue


@app.post("/api/my_prs/{number}/threads/{thread_id}/reply")
async def reply_to_thread(number: int, thread_id: str, payload: ThreadReplyIn):
    """Send a reply the user has approved to one outstanding thread."""
    body = (payload.body or "").strip()
    if not body:
        raise HTTPException(400, "nothing to send")
    try:
        if payload.kind == "inline":
            gh.reply_to_inline_comment(number, thread_id, body)
        else:
            gh.post_issue_comment(number, body)
    except Exception as e:
        raise HTTPException(500, f"gh post failed: {e}")
    with db.conn() as c:
        c.execute(
            "UPDATE my_pr_actions SET status='replied' WHERE pr_number=? AND thread_id=?",
            (number, thread_id),
        )
    db.log_action(number, "my_pr_replied", f"thread {thread_id}")
    return {"ok": True}


class RefineIn(BaseModel):
    note: str


@app.post("/api/my_prs/{number}/threads/{thread_id}/refine")
async def refine_thread(number: int, thread_id: str, payload: RefineIn):
    """Turn "I'll take this bit but not that bit" into something actionable."""
    note = (payload.note or "").strip()
    if not note:
        raise HTTPException(400, "say what you want to do with it first")
    result = await my_prs.refine(number, thread_id, note)
    if not result["ok"]:
        raise HTTPException(400, result["error"])
    return result


@app.post("/api/my_prs/{number}/threads/{thread_id}/handoff")
async def handoff_thread(number: int, thread_id: str):
    """Write the refined instruction out for a Claude Code session to pick up."""
    result = my_prs.write_handoff(number, thread_id)
    if not result["ok"]:
        raise HTTPException(400, result["error"])
    return result


@app.get("/api/my_prs/{number}/threads/{thread_id}/chat")
async def get_thread_chat(number: int, thread_id: str):
    return {"messages": chat.scoped_history(f"thread:{number}:{thread_id}")}


@app.post("/api/my_prs/{number}/threads/{thread_id}/chat")
async def send_thread_chat(number: int, thread_id: str, payload: ChatIn):
    """Talk through one comment: agree with it, argue with it, or understand it.

    Scoped to the thread rather than the PR so the conversation stays about the
    one decision, and so several threads on the same PR do not bleed together.
    """
    msg = (payload.message or "").strip()
    if not msg:
        raise HTTPException(400, "message is empty")
    pr, thread = my_prs.find_thread(number, thread_id)
    if pr is None:
        raise HTTPException(404, "not one of your open PRs")
    if thread is None:
        raise HTTPException(404, "that thread is no longer outstanding")
    with db.conn() as c:
        row = c.execute(
            "SELECT * FROM my_pr_actions WHERE pr_number=? AND thread_id=?",
            (number, str(thread_id)),
        ).fetchone()
    analysis = dict(row) if row else None
    result = await chat.send_scoped(
        f"thread:{number}:{thread_id}",
        msg,
        lambda m: my_prs.clarifier_seed(number, pr["title"], thread, analysis, m),
    )
    if not result["ok"]:
        raise HTTPException(500, result["error"])
    return result


@app.post("/api/my_prs/{number}/threads/{thread_id}/chat/reset")
async def reset_thread_chat(number: int, thread_id: str):
    chat.scoped_reset(f"thread:{number}:{thread_id}")
    return {"ok": True}


@app.post("/api/my_prs/{number}/threads/{thread_id}/skip")
async def skip_thread(number: int, thread_id: str):
    with db.conn() as c:
        c.execute(
            "UPDATE my_pr_actions SET status='skipped' WHERE pr_number=? AND thread_id=?",
            (number, thread_id),
        )
    db.log_action(number, "my_pr_skipped", f"thread {thread_id}")
    return {"ok": True}


@app.post("/api/my_prs/{number}/merge")
async def merge_my_pr(number: int):
    """Merge one of the user's own PRs.

    Uses the configured merge skill if set, otherwise a plain squash merge
    (see merger.py). Fire-and-forget: returns immediately while the merge runs
    in the background. The sidebar reflects progress via /api/my_prs.
    """
    if number in merger.MERGING:
        raise HTTPException(409, "merge already in progress")
    import asyncio
    asyncio.create_task(merger.merge_pr(number))
    return {"ok": True, "status": "merge_started"}


# ---------- Routes: PRs ----------------------------------------------------
@app.post("/api/prs")
async def add_pr(pr: PRIn):
    with db.conn() as c:
        c.execute(
            """INSERT OR REPLACE INTO prs
               (number, author, title, url, head_sha, jira_key, jira_summary, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'queued')""",
            (pr.number, pr.author, pr.title, pr.url, pr.head_sha, pr.jira_key, pr.jira_summary),
        )
    db.log_action(pr.number, "added", f"{pr.author}: {pr.title}")
    # Reviews are user-triggered only — the PR sits as 'queued' in the UI
    # until the user clicks Review.
    return {"ok": True, "status": "queued"}


@app.post("/api/prs/{number}/findings")
def add_findings(number: int, items: list[FindingIn]):
    with db.conn() as c:
        pr = c.execute("SELECT * FROM prs WHERE number=?", (number,)).fetchone()
        if pr is None:
            raise HTTPException(404, "PR not found")
        for f in items:
            c.execute(
                """INSERT INTO findings
                   (pr_number, severity, file, line, title, message,
                    code_snippet, blast_radius, confidence, fix, suggestion_body,
                    plain_verdict, plain_title, plain_summary, plain_impact_label,
                    plain_impact, plain_body)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (number, f.severity, f.file, f.line, f.title, f.message,
                 f.code_snippet, f.blast_radius, f.confidence, f.fix, f.suggestion_body,
                 f.plain_verdict, f.plain_title, f.plain_summary, f.plain_impact_label,
                 f.plain_impact, f.plain_body),
            )
        c.execute(
            "UPDATE prs SET status='awaiting_user', updated_at=datetime('now') WHERE number=?",
            (number,),
        )
    db.log_action(number, "findings_added", f"{len(items)} findings")
    return {"ok": True, "count": len(items)}


@app.post("/api/prs/{number}/approve")
async def approve(number: int):
    return _do_approve(number, reason="manual")


@app.post("/api/prs/{number}/approve_if_addressed")
async def approve_if_addressed(number: int):
    """Run a Claude check that classifies each posted finding.

    Approves only if every posted finding is resolved (addressed by the diff,
    explained by a reply, or genuinely minor). Otherwise leaves the PR as
    pending_author with the per-finding verdict in the activity log so the
    user can decide what to do next.
    """
    with db.conn() as c:
        pr = c.execute("SELECT * FROM prs WHERE number=?", (number,)).fetchone()
        if pr is None:
            raise HTTPException(404, "PR not found")
    # Truth lives on GitHub — comments may have been posted via `gh` directly
    # without going through the UI's Post button.
    try:
        my_comments = [
            c for c in gh.fetch_all_review_comments(number)
            if c.get("user", {}).get("login") == watchers.SELF_LOGIN
            and not c.get("in_reply_to_id")
        ]
    except Exception as e:
        raise HTTPException(502, f"gh fetch comments failed: {e}")
    if not my_comments:
        raise HTTPException(400, "no inline comments by you on this PR — use Approve instead")

    result = await reviewer.verify_addressed(number)
    if not result.get("ok"):
        # Status was already moved to pending_author by the reviewer on failure.
        return JSONResponse(
            {"ok": False, "error": result.get("error", "unknown error")},
            status_code=502,
        )

    verdict = result["verdict"]
    items = verdict.get("items", []) or []

    if verdict.get("all_resolved"):
        try:
            gh.approve_pr(number)
        except Exception as e:
            with db.conn() as c:
                c.execute(
                    "UPDATE prs SET status='pending_author', updated_at=datetime('now') WHERE number=?",
                    (number,),
                )
            raise HTTPException(500, f"gh approve failed: {e}")
        with db.conn() as c:
            c.execute("DELETE FROM prs WHERE number=?", (number,))
        details = "; ".join(
            f"{i.get('status')}: {i.get('title')}" for i in items
        ) or "no items"
        db.log_action(number, "approve_if_addressed", details)
        watchers.notify(f"Approved #{number} (all addressed)")
        return {"ok": True, "approved": True, "verdict": verdict}

    # Not all resolved — surface the unresolved items in the activity log and
    # park the PR back on the author. The verdict is also returned so the UI
    # can show it to the user.
    unresolved = [i for i in items if i.get("status") == "unresolved"]
    with db.conn() as c:
        c.execute(
            "UPDATE prs SET status='pending_author', updated_at=datetime('now') WHERE number=?",
            (number,),
        )
    for i in unresolved:
        db.log_action(
            number,
            "address_check_unresolved",
            f"{i.get('title')}: {i.get('reasoning', '')}",
        )
    if not unresolved:
        # all_resolved=false but no items flagged unresolved — defensive log
        db.log_action(number, "address_check_blocked", "verdict declined approval")
    return {"ok": True, "approved": False, "verdict": verdict}


@app.post("/api/prs/{number}/review")
async def trigger_review(number: int):
    """Manually kick off a review (or retry after a failure).

    Keeps posted/skipped findings so user decisions are preserved.
    Only removes pending findings that would be duplicated by the new review.
    """
    with db.conn() as c:
        if c.execute("SELECT 1 FROM prs WHERE number=?", (number,)).fetchone() is None:
            raise HTTPException(404, "PR not found")
        c.execute(
            "DELETE FROM findings WHERE pr_number=? AND status='pending'",
            (number,),
        )
        c.execute(
            "UPDATE prs SET status='queued', updated_at=datetime('now') WHERE number=?",
            (number,),
        )
    import asyncio
    asyncio.create_task(reviewer.review_pr(number))
    return {"ok": True, "status": "review_started"}


@app.post("/api/watchers/{name}/run")
async def run_watcher(name: str):
    """Manually trigger a registered watcher and update its watcher_runs row."""
    match = next(((n, i, fn) for n, i, fn in watchers.WATCHERS if n == name), None)
    if match is None:
        raise HTTPException(404, f"unknown watcher: {name}")
    wname, interval, fn = match
    try:
        result = await fn()
    except Exception as e:
        watchers._upsert_run(wname, f"error: {e}", interval)
        raise HTTPException(500, f"{wname} failed: {e}")
    watchers._upsert_run(wname, result, interval)
    return {"ok": True, "result": result}


@app.post("/api/prs/{number}/promote")
async def promote(number: int):
    """Add this PR to the head list. Does not unpin other heads — multiple
    PRs can be pinned at once so the user can review them in parallel. Clears
    any parked flag on this PR.
    """
    with db.conn() as c:
        if c.execute("SELECT 1 FROM prs WHERE number=?", (number,)).fetchone() is None:
            raise HTTPException(404, "PR not found")
        c.execute(
            "UPDATE prs SET pinned_at=datetime('now'), parked=0 WHERE number=?",
            (number,),
        )
    db.log_action(number, "added_to_head", "")
    return {"ok": True}


@app.post("/api/prs/{number}/move_off_head")
async def move_off_head(number: int):
    """Remove this PR from the head slot WITHOUT deleting it or its findings.

    Sets parked=1 so it won't be auto-picked as head again, and clears any
    pinned_at. The PR stays in the pending review list with all its findings
    intact, ready to be promoted again later.
    """
    with db.conn() as c:
        if c.execute("SELECT 1 FROM prs WHERE number=?", (number,)).fetchone() is None:
            raise HTTPException(404, "PR not found")
        c.execute(
            "UPDATE prs SET pinned_at=NULL, parked=1 WHERE number=?",
            (number,),
        )
    db.log_action(number, "moved_off_head", "")
    return {"ok": True}


@app.post("/api/prs/{number}/review_done")
async def review_done(number: int):
    """Manually mark the review as done and hand the PR to the author.

    Used when the user reviewed the PR themselves (possibly posting comments
    directly on GitHub) without posting every finding through the UI. Skips
    any remaining pending findings, moves the PR to pending_author, and
    snapshots follow-up baselines so only activity after this point counts
    as new.
    """
    with db.conn() as c:
        if c.execute("SELECT 1 FROM prs WHERE number=?", (number,)).fetchone() is None:
            raise HTTPException(404, "PR not found")
    # Free the review slot if a review is still in flight — the user has
    # already made their call.
    reviewer.kill_review(number)
    with db.conn() as c:
        c.execute(
            "UPDATE findings SET status='skipped' WHERE pr_number=? AND status='pending'",
            (number,),
        )
        c.execute(
            """UPDATE prs SET status='pending_author', pinned_at=NULL, parked=0,
                 has_new_activity=0, updated_at=datetime('now') WHERE number=?""",
            (number,),
        )
    try:
        fresh = gh.get_pr(number)
        review_at = gh.latest_review_comment_at(number, watchers.SELF_LOGIN)
        issue_at = gh.latest_issue_comment_at(number, watchers.SELF_LOGIN)
        with db.conn() as c:
            c.execute(
                """UPDATE prs SET
                     head_sha=?,
                     last_seen_commit_sha=?,
                     last_seen_review_comment_at=?,
                     last_seen_issue_comment_at=?
                   WHERE number=?""",
                (fresh["headRefOid"], fresh["headRefOid"], review_at, issue_at, number),
            )
    except Exception:
        pass
    db.log_action(number, "review_done", "moved to pending_author")
    return {"ok": True, "status": "pending_author"}


@app.post("/api/prs/{number}/dismiss")
async def dismiss(number: int):
    """Delete without approving. Used when a PR was closed/merged externally."""
    # Kill any in-flight review for this PR so it doesn't keep holding
    # a semaphore slot and jam the queue behind a row that no longer exists.
    reviewer.kill_review(number)
    with db.conn() as c:
        c.execute("DELETE FROM prs WHERE number=?", (number,))
    db.log_action(number, "dismissed", "")
    return {"ok": True}


def _do_approve(number: int, reason: str):
    with db.conn() as c:
        pr = c.execute("SELECT * FROM prs WHERE number=?", (number,)).fetchone()
        if pr is None:
            raise HTTPException(404, "PR not found")
    # Kill any in-flight review — we're approving now, no need for findings.
    reviewer.kill_review(number)
    try:
        gh.approve_pr(number)
    except Exception as e:
        raise HTTPException(500, f"gh approve failed: {e}")
    with db.conn() as c:
        c.execute("DELETE FROM prs WHERE number=?", (number,))  # cascades findings
    db.log_action(number, "approved", reason)
    watchers.notify(f"Approved #{number}")
    return {"ok": True}


# ---------- Routes: chat ---------------------------------------------------
class ChatPostIn(BaseModel):
    body: str


@app.get("/api/prs/{number}/chat")
async def get_chat(number: int):
    return {"messages": chat.history(number)}


@app.post("/api/prs/{number}/chat")
async def send_chat(number: int, payload: ChatIn):
    msg = (payload.message or "").strip()
    if not msg:
        raise HTTPException(400, "message is empty")
    result = await chat.send(number, msg)
    if not result["ok"]:
        raise HTTPException(500, result["error"])
    return result


@app.post("/api/prs/{number}/chat/reset")
async def reset_chat(number: int):
    chat.reset(number)
    return {"ok": True}


@app.post("/api/prs/{number}/chat/post")
async def post_from_chat(number: int, payload: ChatPostIn):
    """Send a body Claude drafted in the chat to the PR as a general comment.

    Claude has no tool that can reach GitHub, so this endpoint is the only path
    from the conversation to the PR, and it only runs when the user clicks.
    """
    body = (payload.body or "").strip()
    if not body:
        raise HTTPException(400, "nothing to post")
    try:
        cid = gh.post_issue_comment(number, body)
    except Exception as e:
        raise HTTPException(500, f"gh post failed: {e}")
    db.log_action(number, "chat_comment_posted", f"comment {cid}")
    watchers.notify(f"Posted a chat comment on #{number}")
    return {"ok": True, "comment_id": cid}


# ---------- Routes: findings ----------------------------------------------
@app.post("/api/findings/{fid}/post")
async def post_finding(fid: int):
    with db.conn() as c:
        f = c.execute("SELECT * FROM findings WHERE id=?", (fid,)).fetchone()
        if f is None:
            raise HTTPException(404, "finding not found")
        pr = c.execute("SELECT * FROM prs WHERE number=?", (f["pr_number"],)).fetchone()
    if not f["file"] or not f["line"]:
        raise HTTPException(400, "finding lacks file/line for inline posting")
    body = f["suggestion_body"] or f["message"]
    # Always fetch the latest head SHA — the stored one may be stale if the
    # author pushed new commits after we reviewed.
    try:
        fresh = gh.get_pr(pr["number"])
        head_sha = fresh["headRefOid"]
    except Exception as e:
        raise HTTPException(500, f"gh get_pr failed: {e}")
    try:
        cid = gh.post_inline_comment(
            pr["number"], body, f["file"], f["line"], head_sha,
        )
    except Exception as e:
        raise HTTPException(500, f"gh post failed: {e}")
    # Technical comment is up. Hang the plain-English retelling underneath it
    # as a threaded reply, so the author reads the precise version first and
    # the human one second. A failure here must not undo the comment we just
    # posted, so it is logged rather than raised.
    if f["plain_body"]:
        try:
            gh.reply_to_inline_comment(pr["number"], cid, f["plain_body"])
        except Exception as e:
            db.log_action(pr["number"], "plain_reply_failed", str(e)[:500])
    # Keep DB in sync with the SHA we just used.
    with db.conn() as c:
        c.execute("UPDATE prs SET head_sha=? WHERE number=?", (head_sha, pr["number"]))
    with db.conn() as c:
        c.execute(
            "UPDATE findings SET status='posted', github_comment_id=? WHERE id=?",
            (cid, fid),
        )
        # If no pending findings remain, mark PR as pending_author
        remaining = c.execute(
            "SELECT COUNT(*) as n FROM findings WHERE pr_number=? AND status='pending'",
            (pr["number"],),
        ).fetchone()["n"]
        if remaining == 0:
            c.execute(
                "UPDATE prs SET status='pending_author', updated_at=datetime('now') WHERE number=?",
                (pr["number"],),
            )
    # Refresh follow-up baselines so we only detect replies posted AFTER this
    # comment. The inline comment we just created will be max(updated_at) and
    # it's by us — but others' replies will be strictly greater.
    try:
        review_at = gh.latest_review_comment_at(pr["number"], watchers.SELF_LOGIN)
        issue_at = gh.latest_issue_comment_at(pr["number"], watchers.SELF_LOGIN)
        with db.conn() as c:
            c.execute(
                """UPDATE prs SET
                     last_seen_review_comment_at=?,
                     last_seen_issue_comment_at=?
                   WHERE number=?""",
                (review_at, issue_at, pr["number"]),
            )
    except Exception:
        pass
    db.log_action(pr["number"], "posted", f"finding #{fid}")
    return {"ok": True, "comment_id": cid}


@app.post("/api/findings/{fid}/skip")
async def skip_finding(fid: int):
    with db.conn() as c:
        f = c.execute("SELECT * FROM findings WHERE id=?", (fid,)).fetchone()
        if f is None:
            raise HTTPException(404, "finding not found")
        c.execute("UPDATE findings SET status='skipped' WHERE id=?", (fid,))
        remaining = c.execute(
            "SELECT COUNT(*) as n FROM findings WHERE pr_number=? AND status='pending'",
            (f["pr_number"],),
        ).fetchone()["n"]
        if remaining == 0:
            # If anything was posted, the PR is waiting on the author — move it
            # off head. If nothing was posted, keep it on head so the user can
            # approve or trigger a fresh review.
            posted = c.execute(
                "SELECT COUNT(*) as n FROM findings WHERE pr_number=? AND status='posted'",
                (f["pr_number"],),
            ).fetchone()["n"]
            if posted > 0:
                c.execute(
                    "UPDATE prs SET status='pending_author', updated_at=datetime('now') WHERE number=?",
                    (f["pr_number"],),
                )
    db.log_action(f["pr_number"], "skipped", f"finding #{fid}")
    return {"ok": True}
