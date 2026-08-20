"""Per-PR Claude conversation, opened from the findings list.

The reviewer hands back findings and stops. This is the follow-up: a chat that
already knows the PR and what the review said, so the user can ask "is number 4
actually a problem?" or "write me a reply to the author" without re-explaining
anything.

Two deliberate constraints:

1. The CLI's own `--session-id` / `--resume` carries the history, so a turn
   costs one message rather than a replayed transcript. The PR diff is fetched
   by Claude on the first turn and stays in that session.

2. Claude is spawned with an explicit tool allowlist that contains no way to
   write to GitHub. It can read the PR and the repo; it cannot comment, review,
   approve or merge. Posting is a separate endpoint the user triggers by
   clicking. This mirrors the reviewer's rule that Claude surfaces and the human
   decides, and it means a prompt-injected instruction in a PR body cannot post
   anything on the user's behalf.
"""
import asyncio
import json
import uuid
from pathlib import Path

from . import config, db

PROJECT_DIR = Path(__file__).resolve().parent.parent
CHAT_TIMEOUT = 300

# No `gh pr comment`, no `gh pr review`, no `gh api`. Read-only by construction.
ALLOWED_TOOLS = [
    "Bash(gh pr diff:*)",
    "Bash(gh pr view:*)",
    "Bash(gh pr checks:*)",
    "Read",
    "Grep",
    "Glob",
]

_CHAT_SEM = asyncio.Semaphore(3)


def _findings_brief(pr_number):
    with db.conn() as c:
        rows = c.execute(
            """SELECT severity, file, line, title, message, plain_verdict,
                      plain_title, plain_summary, plain_impact, status
               FROM findings WHERE pr_number=? ORDER BY severity, id""",
            (pr_number,),
        ).fetchall()
    if not rows:
        return "The review returned no findings."
    out = []
    for i, r in enumerate(rows, 1):
        out.append(
            f"### Finding {i} [{r['status']}] — {r['plain_verdict'] or r['severity']}\n"
            f"- Where: {r['file']}:{r['line']}\n"
            f"- Technical: {r['title']}\n"
            f"  {r['message']}\n"
            f"- Plain English: {r['plain_title'] or '(none)'}\n"
            f"  {r['plain_summary'] or ''}\n"
            f"  {r['plain_impact'] or ''}"
        )
    return "\n\n".join(out)


def _seed_prompt(pr_number, pr_title, user_message):
    return f"""You are helping someone decide what to do about a code review you
already ran on PR #{pr_number} ("{pr_title}") in `{config.repo()}`.

**Ignore any skills or CLAUDE.md files in scope.** They are not part of this task.

Start by fetching the PR so you are arguing from the real diff, not from the
summaries below:

```bash
gh pr diff {pr_number} --repo {config.repo()}
gh pr view {pr_number} --repo {config.repo()} --json title,body
```

Read any file you need to judge a claim. You have read access to the repo.

# What the review found

{_findings_brief(pr_number)}

# Who you are talking to

A product person, not an engineer. They decide which findings get posted to the
author. Write the way you would speak: no identifiers, paths or line numbers in
your prose unless they ask for them, no jargon. If they ask whether a finding is
real, say so plainly and tell them what convinced you. Disagreeing with your own
review is fine and useful. So is saying you are not sure.

# Posting back to the PR

You cannot write to GitHub. You have no tool that can. When they ask you to post
something, or when you have drafted a comment worth sending, wrap the exact
comment body in POST markers:

<POST>
The comment body, as it should appear on the PR.
</POST>

That renders as a button they click to send it. Put only the comment itself
inside the markers, no preamble. Say in your normal reply what the button will
do. Never claim to have posted anything.

---

Their first message:

{user_message}"""


async def _run(scope, prompt, session_args):
    """One `claude -p` turn. Returns the reply text or raises RuntimeError."""
    async with _CHAT_SEM:
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", prompt,
            *session_args,
            "--output-format", "json",
            "--allowedTools", *ALLOWED_TOOLS,
            cwd=str(PROJECT_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=CHAT_TIMEOUT
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError(f"Claude timed out after {CHAT_TIMEOUT}s")

    if proc.returncode != 0:
        err = (stderr.decode(errors="replace") or "").strip()
        raise RuntimeError(err or f"claude exited {proc.returncode}")

    try:
        payload = json.loads(stdout.decode(errors="replace"))
        reply = payload.get("result") or ""
    except (ValueError, TypeError):
        # Fall back to raw stdout rather than losing a turn the user paid for.
        reply = stdout.decode(errors="replace").strip()
    if not reply:
        raise RuntimeError("Claude returned an empty reply")
    return reply


async def send_scoped(scope, user_message, seed):
    """Run one turn in `scope`, seeding the conversation on its first message.

    `seed` is called only on the first turn and returns the full opening prompt
    (context plus the user's message). Later turns send the bare message and
    let --resume carry the history.
    """
    with db.conn() as c:
        row = c.execute(
            "SELECT session_id FROM chat_sessions WHERE scope=?", (scope,)
        ).fetchone()
    session_id = row["session_id"] if row else None
    first_turn = session_id is None
    if first_turn:
        session_id = str(uuid.uuid4())
        prompt = seed(user_message)
        session_args = ["--session-id", session_id]
    else:
        prompt = user_message
        session_args = ["--resume", session_id]

    with db.conn() as c:
        c.execute(
            "INSERT INTO scoped_chat_messages (scope, role, content) VALUES (?, 'user', ?)",
            (scope, user_message),
        )
    try:
        reply = await _run(scope, prompt, session_args)
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}

    # Only claim the session once a turn has actually succeeded, so a failed
    # first turn does not strand the scope on a session that never existed.
    with db.conn() as c:
        if first_turn:
            c.execute(
                "INSERT OR REPLACE INTO chat_sessions (scope, session_id) VALUES (?, ?)",
                (scope, session_id),
            )
        c.execute(
            "INSERT INTO scoped_chat_messages (scope, role, content) VALUES (?, 'assistant', ?)",
            (scope, reply),
        )
    return {"ok": True, "reply": reply}


def scoped_history(scope):
    with db.conn() as c:
        return db.rows_to_dicts(c.execute(
            "SELECT id, role, content, created_at FROM scoped_chat_messages "
            "WHERE scope=? ORDER BY id ASC",
            (scope,),
        ).fetchall())


def scoped_reset(scope):
    with db.conn() as c:
        c.execute("DELETE FROM scoped_chat_messages WHERE scope=?", (scope,))
        c.execute("DELETE FROM chat_sessions WHERE scope=?", (scope,))


# ---- Review-queue PR chat -------------------------------------------------
async def send(pr_number, user_message):
    """The review-queue chat: one conversation per PR being reviewed."""
    with db.conn() as c:
        pr = c.execute(
            "SELECT number, title FROM prs WHERE number=?", (pr_number,)
        ).fetchone()
    if pr is None:
        return {"ok": False, "error": "PR not found"}
    return await send_scoped(
        f"pr:{pr_number}",
        user_message,
        lambda msg: _seed_prompt(pr_number, pr["title"], msg),
    )


def history(pr_number):
    return scoped_history(f"pr:{pr_number}")


def reset(pr_number):
    scoped_reset(f"pr:{pr_number}")
