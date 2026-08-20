"""The user's own open PRs: what state each is in, and what to do next.

The main queue answers "what should I review?". This answers the other half of
the day: "what is blocked on me?". Every open PR of the user's is sorted into
one bucket, and PRs carrying unanswered feedback get a Claude pass that says,
per comment, whether it needs a code change, a reply, or nothing.

Buckets:

  ready       approved, checks green, nothing outstanding -> merge it
  comments    someone is waiting on a reply from you
  push        healthy but nobody is looking at it -> chase a reviewer
  not_ready   draft, or failing checks; the ball is in your court first

`not_ready` is a fourth bucket beyond the three asked for, because a draft and
a red build genuinely belong in neither "ready to go" nor "push for a review",
and folding them into either would tell the user to do the wrong thing.
"""
import asyncio
import json
import subprocess
from pathlib import Path

from . import config, db, gh

PROJECT_DIR = Path(__file__).resolve().parent.parent

# Bots whose comments are pipeline output, not feedback. Nothing these post
# ever counts as awaiting a reply. `cezbot` is deliberately NOT here: it is a
# reviewer, and its findings need answering like anyone else's.
NOISE_BOTS = {
    "swarmia[bot]",
    "github-actions[bot]",
    "codecov[bot]",
    "sonarcloud[bot]",
    "dependabot[bot]",
}

# Not everything cezbot posts is feedback. Its run summaries are pointers to
# the inline comments that carry the actual findings, and its TriviAI verdicts
# are an automated is-this-worth-reviewing label addressed to the team, not a
# question for the author. Neither is something to reply to; the real findings
# arrive as inline comments and are picked up there.
_CEZBOT_NOISE_MARKERS = (
    "<!-- triviai",
    "<!-- cezbot-run-summary",
    "found no new issues",
    "no new issues found",
)

ANALYSIS_TIMEOUT = 420
_ANALYSIS_SEM = asyncio.Semaphore(2)


def _gh_json(args):
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "").strip())
    return json.loads(r.stdout or "[]")


def _is_noise(login, body):
    if login in NOISE_BOTS:
        return True
    if login == "cezbot[bot]":
        low = (body or "").lower()
        return any(m in low for m in _CEZBOT_NOISE_MARKERS)
    return False


def _checks_state(rollup):
    """green | red | pending | none, collapsed from the per-check rollup."""
    if not rollup:
        return "none"
    states = [(c.get("conclusion") or c.get("state") or "").upper() for c in rollup]
    if any(s in ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED") for s in states):
        return "red"
    if any(s in ("PENDING", "IN_PROGRESS", "QUEUED", "EXPECTED", "") for s in states):
        return "pending"
    return "green"


def _threads(number, self_login):
    """Comment threads on the PR that are waiting on the user.

    Inline comments are grouped by their reply chain; a thread is outstanding
    when its most recent comment is not the user's. PR-level comments have no
    threading, so one is outstanding when the user has posted nothing since.
    """
    inline = _gh_json([
        "gh", "api", "--paginate", f"repos/{config.repo()}/pulls/{number}/comments",
    ])
    issues = _gh_json([
        "gh", "api", "--paginate", f"repos/{config.repo()}/issues/{number}/comments",
    ])

    chains = {}
    for c in inline:
        root = c.get("in_reply_to_id") or c["id"]
        chains.setdefault(root, []).append(c)

    out = []
    for root, msgs in chains.items():
        msgs.sort(key=lambda m: m["created_at"])
        real = [m for m in msgs if not _is_noise(m["user"]["login"], m.get("body"))]
        if not real:
            continue
        last = msgs[-1]
        if last["user"]["login"] == self_login:
            continue  # user replied last, ball is with them
        first = real[0]
        out.append({
            "kind": "inline",
            "root_id": root,
            "author": first["user"]["login"],
            "path": first.get("path"),
            "line": first.get("line") or first.get("original_line"),
            "created_at": first["created_at"],
            "body": "\n\n---\n\n".join(
                f"{m['user']['login']}: {m.get('body') or ''}" for m in msgs
            ),
        })

    last_self = max(
        (c["created_at"] for c in issues if c["user"]["login"] == self_login),
        default="",
    )
    for c in issues:
        login = c["user"]["login"]
        if login == self_login or _is_noise(login, c.get("body")):
            continue
        if last_self and c["created_at"] < last_self:
            continue  # user has spoken since
        out.append({
            "kind": "issue",
            "root_id": c["id"],
            "author": login,
            "path": None,
            "line": None,
            "created_at": c["created_at"],
            "body": c.get("body") or "",
        })

    out.sort(key=lambda t: t["created_at"])
    return out


def _categorise(pr, checks, threads):
    if pr["is_draft"] or checks == "red":
        return "not_ready"
    if threads:
        return "comments"
    if pr["review_decision"] == "APPROVED" and checks in ("green", "none"):
        return "ready"
    return "push"


def gather(self_login):
    """Every open PR of the user's, bucketed, with outstanding threads attached."""
    prs = gh.list_my_open_prs(self_login)
    if not prs:
        return []
    detail = {
        p["number"]: p for p in _gh_json([
            "gh", "pr", "list", "--repo", config.repo(), "--author", self_login,
            "--state", "open", "--limit", "50",
            "--json", "number,statusCheckRollup,updatedAt,additions,deletions",
        ])
    }
    with db.conn() as c:
        saved = {
            (r["pr_number"], r["thread_id"]): r
            for r in c.execute("SELECT * FROM my_pr_actions").fetchall()
        }
        requests = {
            r["pr_number"]: dict(r)
            for r in c.execute("SELECT * FROM review_requests").fetchall()
        }
        refinements = {
            (r["pr_number"], r["thread_id"]): dict(r)
            for r in c.execute("SELECT * FROM thread_refinements").fetchall()
        }

    out = []
    for p in prs:
        d = detail.get(p["number"], {})
        checks = _checks_state(d.get("statusCheckRollup"))
        try:
            threads = _threads(p["number"], self_login)
        except Exception as e:  # noqa: BLE001 - one bad PR must not blank the tab
            threads = []
            p["threads_error"] = str(e)
        for t in threads:
            rec = saved.get((p["number"], str(t["root_id"])))
            t["analysis"] = dict(rec) if rec else None
            t["refinement"] = refinements.get((p["number"], str(t["root_id"])))
        out.append({
            **p,
            "checks": checks,
            "updated_at": d.get("updatedAt"),
            "size": (d.get("additions") or 0) + (d.get("deletions") or 0),
            "threads": threads,
            "category": _categorise(p, checks, threads),
            "review_request": requests.get(p["number"]),
        })
    return out


_ANALYSIS_PROMPT = """You are triaging the feedback on someone's own pull
request so they can clear it quickly. PR #{number} ("{title}") in `{repo}`.

**Ignore any skills or CLAUDE.md files in scope.** They are not part of this task.

Read the PR before judging any comment:

```bash
gh pr diff {number} --repo {repo}
gh pr view {number} --repo {repo} --json title,body
```

# Outstanding comments

These are the threads where the last word was not the author's, so each is
waiting on them. `cezbot` is an automated reviewer; treat its findings on
their merits, exactly as you would a colleague's.

{threads}

# What to produce

For each comment, decide what it actually needs:

- `code_fix` — the comment is right and the code should change.
- `reply` — no code change needed, but it deserves an answer: a question to
  answer, a disagreement to make, or context the commenter is missing.
- `no_action` — informational, already handled, or resolved by a later commit.
  Say so and move on.

Then write, for each:

- `summary` — what the commenter is actually asking, in plain English. No
  identifiers, paths or line numbers in this field. The reader is not an
  engineer. Say what it means for the change, not what the code says.
- `recommendation` — one or two sentences on what you would do and why. If you
  think the comment is wrong, say that plainly.
- `reply_draft` — for `reply`, the message to send, written as the PR author
  speaking to the commenter. Direct and courteous, no throat-clearing, no
  apologising for existing. If you are pushing back, give the actual reason.
  For `code_fix`, the short holding reply that says what you are going to do.
  Empty string for `no_action`.
- `fix_prompt` — a self-contained instruction someone could hand to Claude
  Code in the repo to make the change. Name the file and what to change, state
  how to verify it, and mention the test to add or update.

  Write this for `code_fix` **and** for `reply`. On a `reply` you are arguing
  that no change is needed, but the author may read the thread and decide the
  commenter had a point after all. Give them the route to act on it without
  coming back to ask. Write it as the change the commenter is asking for, not
  as a defence of the current code.

  Empty string only for `no_action`, where the code is already right or a later
  commit has handled it.
- `confidence` — low | medium | high, on your read of what the comment needs.

Both fields matter on a `reply`. The author has two ways forward — push back,
or concede and change it — and the point of this is that they do not have to
work the second one out for themselves.

Output only a JSON array inside <ACTIONS>...</ACTIONS>, one object per comment,
in the same order, each carrying the `thread_id` it belongs to:

<ACTIONS>
[{{"thread_id": "...", "action": "reply", "summary": "...", "recommendation": "...",
   "reply_draft": "...", "fix_prompt": "...", "confidence": "high"}}]
</ACTIONS>

No prose outside the markers."""


async def triage(number, title, threads):
    """Ask Claude what each outstanding thread needs. Returns the parsed items.

    Split out from `analyse` so the prompt and the JSON contract can be
    exercised against any PR, not only the current user's.
    """
    blocks = []
    for t in threads:
        where = f"{t['path']}:{t['line']}" if t["path"] else "PR conversation"
        blocks.append(
            f"## thread_id {t['root_id']}\n"
            f"- From: {t['author']}\n"
            f"- Where: {where}\n\n{t['body']}"
        )
    prompt = _ANALYSIS_PROMPT.format(
        number=number, title=title, repo=config.repo(),
        threads="\n\n".join(blocks),
    )

    async with _ANALYSIS_SEM:
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", prompt,
            "--allowedTools", "Bash(gh pr diff:*)", "Bash(gh pr view:*)",
            "Read", "Grep", "Glob",
            cwd=str(PROJECT_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=ANALYSIS_TIMEOUT
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {"ok": False, "error": f"timed out after {ANALYSIS_TIMEOUT}s"}

    if proc.returncode != 0:
        return {"ok": False, "error": (stderr.decode(errors="replace") or "").strip()}

    out = stdout.decode(errors="replace")
    if "<ACTIONS>" not in out:
        return {"ok": False, "error": "no <ACTIONS> block in output"}
    raw = out.split("<ACTIONS>", 1)[1].split("</ACTIONS>", 1)[0].strip()
    try:
        items = json.loads(raw)
    except ValueError as e:
        return {"ok": False, "error": f"invalid JSON: {e}"}
    return {"ok": True, "items": items}


async def analyse(number):
    """Triage one of the user's own PRs and store the result."""
    prs = {p["number"]: p for p in gather(config.self_login())}
    pr = prs.get(number)
    if pr is None:
        return {"ok": False, "error": "not one of your open PRs"}
    if not pr["threads"]:
        return {"ok": False, "error": "nothing outstanding on this PR"}

    res = await triage(number, pr["title"], pr["threads"])
    if not res["ok"]:
        return res
    items = res["items"]

    with db.conn() as c:
        for it in items:
            c.execute(
                """INSERT INTO my_pr_actions
                     (pr_number, thread_id, action, summary, recommendation,
                      reply_draft, fix_prompt, confidence, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                   ON CONFLICT(pr_number, thread_id) DO UPDATE SET
                     action=excluded.action, summary=excluded.summary,
                     recommendation=excluded.recommendation,
                     reply_draft=excluded.reply_draft,
                     fix_prompt=excluded.fix_prompt,
                     confidence=excluded.confidence,
                     status='pending', created_at=datetime('now')""",
                (
                    number, str(it.get("thread_id")), it.get("action", "reply"),
                    it.get("summary", ""), it.get("recommendation", ""),
                    it.get("reply_draft", ""), it.get("fix_prompt", ""),
                    it.get("confidence", "medium"),
                ),
            )
    db.log_action(number, "my_pr_triaged", f"{len(items)} threads")
    return {"ok": True, "count": len(items)}

_REQUEST_PROMPT = """Write the one line of context that goes above a review
request for PR #{number} ("{title}") in `{repo}`.

**Ignore any skills or CLAUDE.md files in scope.** They are not part of this task.

Read the PR first:

```bash
gh pr diff {number} --repo {repo}
gh pr view {number} --repo {repo} --json title,body
```

This is being dropped into a busy team channel. The title and a link sit
underneath it, so do not restate the title. The line's only job is to tell a
colleague scrolling past why they should pick this up, in the words they would
use themselves.

What works:

- Where it sits in a bigger piece of work. "Last bit of adding time tracking to
  events." "First of three on the CSV importer."
- What it unblocks, if that is the reason to look now.
- A warning if the change is riskier or larger than the title suggests.

What does not:

- Restating the title in different words.
- "This PR adds..." or "This change..." — start with the substance.
- Identifiers, file paths, class names, line counts, percentages.
- Selling it. No "quick one", no "should be straightforward", no "easy review"
  unless it genuinely is trivial and you can say why in the same breath.

One sentence. Two only if the second earns it. Sentence case, British English,
no em-dashes, no trailing full stop if it reads as a fragment.

If the PR is part of a numbered series or names a parent ticket in its body, say
so — that is usually the most useful thing a reviewer can know.

Output only the line, wrapped in markers, nothing else:

<SUMMARY>
your line here
</SUMMARY>"""


async def draft_review_request(number):
    """Write and store the review-request blurb for one of the user's PRs."""
    prs = {p["number"]: p for p in gather(config.self_login())}
    pr = prs.get(number)
    if pr is None:
        return {"ok": False, "error": "not one of your open PRs"}

    prompt = _REQUEST_PROMPT.format(
        number=number, title=pr["title"], repo=config.repo()
    )
    async with _ANALYSIS_SEM:
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", prompt,
            "--allowedTools", "Bash(gh pr diff:*)", "Bash(gh pr view:*)",
            "Read", "Grep", "Glob",
            cwd=str(PROJECT_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {"ok": False, "error": "timed out drafting the summary"}

    if proc.returncode != 0:
        return {"ok": False, "error": (stderr.decode(errors="replace") or "").strip()}

    out = stdout.decode(errors="replace")
    if "<SUMMARY>" not in out:
        return {"ok": False, "error": "no <SUMMARY> block in output"}
    summary = out.split("<SUMMARY>", 1)[1].split("</SUMMARY>", 1)[0].strip()
    if not summary:
        return {"ok": False, "error": "empty summary"}

    with db.conn() as c:
        c.execute(
            """INSERT INTO review_requests (pr_number, summary, title, url, status)
               VALUES (?, ?, ?, ?, 'draft')
               ON CONFLICT(pr_number) DO UPDATE SET
                 summary=excluded.summary, title=excluded.title,
                 url=excluded.url, status='draft', sent_at=NULL,
                 created_at=datetime('now')""",
            (number, summary, pr["title"], pr["url"]),
        )
    db.log_action(number, "review_request_drafted", summary[:200])
    return {"ok": True, "summary": summary, "title": pr["title"], "url": pr["url"]}


def format_request(summary, title, url):
    """The message as it goes into the channel.

    Summary, then title, then the bare link on its own line so Teams unfurls it
    into a card. The card repeats the title, which is why the title line is not
    itself a hyperlink: a linked title plus an unfurled card reads as a mistake.
    """
    return f"{summary}\n\n{title}\n{url}"


def send_to_teams(number, summary, title, url):
    """POST the message to the configured Teams channel webhook."""
    hook = config.teams_webhook_url()
    if not hook:
        return {"ok": False, "error": "no Teams webhook configured"}
    text = format_request(summary, title, url)
    body = json.dumps({"text": text})
    r = subprocess.run(
        ["curl", "-sS", "-X", "POST", "-H", "Content-Type: application/json",
         "-d", body, "--max-time", "30", "-w", "\n%{http_code}", hook],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return {"ok": False, "error": (r.stderr or "curl failed").strip()}
    parts = (r.stdout or "").rsplit("\n", 1)
    code = parts[-1].strip()
    if not code.startswith("2"):
        return {"ok": False, "error": f"Teams returned HTTP {code}: {parts[0][:300]}"}
    with db.conn() as c:
        c.execute(
            "UPDATE review_requests SET status='sent', sent_at=datetime('now') "
            "WHERE pr_number=?",
            (number,),
        )
    db.log_action(number, "review_request_sent", config.teams_channel_label())
    return {"ok": True}

_CLARIFIER_PROMPT = """You are helping the author of PR #{number} ("{title}") in
`{repo}` decide what to do about one comment on it.

**Ignore any skills or CLAUDE.md files in scope.** They are not part of this task.

Read the PR before arguing anything:

```bash
gh pr diff {number} --repo {repo}
gh pr view {number} --repo {repo} --json title,body
```

# The comment

From {author}{where}:

{body}

# What was already suggested

Read as: needs {action}.

{summary}

{recommendation}

# Who you are talking to

The author of the PR, and not an engineer. They have read the suggestion above
and want to think about it rather than act on it straight away. Usually that
means one of:

- They agree with the commenter and want to know what changing it involves.
- They think the commenter is wrong and want to check that instinct before
  saying so.
- They do not follow what the comment is actually asking for.

Answer in plain English. No identifiers, file paths or line numbers in your
prose unless they ask. Give a real opinion and change it when they make a good
point. If the earlier suggestion was wrong, say so plainly.

# What you can produce

You cannot write to GitHub and you cannot edit the repo. Two markers are
available, and they render as buttons:

Wrap a message to send to the commenter in reply markers:

<REPLY>
the message, as the PR author speaking to the commenter
</REPLY>

Wrap an instruction for making the change in fix markers. Self-contained, names
the file and the change, says how to verify it and what test to add:

<FIX>
the instruction to hand to Claude Code in the repo
</FIX>

Use whichever fits what they asked. Both, when they are still deciding and want
to see each option. Neither, when they just asked a question. Only ever put the
artefact itself inside the markers, and say in your normal reply what each
button will do."""


def clarifier_seed(number, title, thread, analysis, user_message):
    """Opening prompt for the per-thread clarifier conversation."""
    where = f" on {thread['path']}:{thread['line']}" if thread.get("path") else ""
    return _CLARIFIER_PROMPT.format(
        number=number, title=title, repo=config.repo(),
        author=thread["author"], where=where, body=thread["body"],
        action=(analysis or {}).get("action", "a decision"),
        summary=(analysis or {}).get("summary", "(no summary was produced)"),
        recommendation=(analysis or {}).get("recommendation", ""),
    ) + f"\n\n---\n\nTheir first message:\n\n{user_message}"


def find_thread(number, thread_id):
    """Locate one outstanding thread plus its stored triage, or (None, None)."""
    prs = {p["number"]: p for p in gather(config.self_login())}
    pr = prs.get(number)
    if pr is None:
        return None, None
    for t in pr["threads"]:
        if str(t["root_id"]) == str(thread_id):
            return pr, t
    return pr, None

HANDOFF_DIR = Path.home() / ".pr-watcher" / "handoffs"

_REFINE_PROMPT = """The author of PR #{number} ("{title}") in `{repo}` partly
agrees with a comment on it. Turn what they have said into an instruction
someone can act on.

**Ignore any skills or CLAUDE.md files in scope.** They are not part of this task.

Read the PR so the instruction is grounded in the real code:

```bash
gh pr diff {number} --repo {repo}
gh pr view {number} --repo {repo} --json title,body
```

# The comment

From {author}{where}:

{body}

# What the author has decided, in their words

{note}

# The line they have drawn is the whole point

They are taking some of that comment and not the rest. Your job is to carry
that split through exactly as they set it, not to relitigate it.

- Do not widen the scope. If they are taking one of three suggestions, the
  instruction covers one.
- Do not quietly reintroduce the parts they declined, and do not soften them
  into "consider also".
- If their note is ambiguous about a specific part, pick the narrower reading
  and say in `notes` which way you read it.
- If doing the part they accepted genuinely forces a change they did not
  mention — a test that stops compiling, a caller that breaks — include it and
  flag it in `notes`. That is a consequence, not an expansion.
- If what they have asked for will not work, say so in `notes`. Still write the
  instruction for what they asked.

# Output

Two blocks and nothing else.

The instruction, self-contained, for someone working in a fresh session with no
knowledge of this conversation. State the file and the change, what to leave
alone, how to verify, and which test to add or update. Name what is
deliberately out of scope so nobody helpfully adds it back:

<INSTRUCTION>
...
</INSTRUCTION>

The reply to the commenter, as the author speaking. It must say plainly which
part is being taken and which is not, and why. Do not thank them twice, do not
apologise, do not hedge the refusal into vagueness. If they were right about
something, say that clearly:

<REPLY>
...
</REPLY>

Anything you want the author to know that belongs in neither block goes here.
Omit it entirely if there is nothing worth saying:

<NOTES>
...
</NOTES>"""


def _block(text, tag):
    open_t, close_t = f"<{tag}>", f"</{tag}>"
    if open_t not in text or close_t not in text:
        return ""
    return text.split(open_t, 1)[1].split(close_t, 1)[0].strip()


async def refine(number, thread_id, note):
    """Turn the author's partial-agreement note into an instruction and a reply."""
    pr, thread = find_thread(number, thread_id)
    if pr is None:
        return {"ok": False, "error": "not one of your open PRs"}
    if thread is None:
        return {"ok": False, "error": "that thread is no longer outstanding"}

    where = f" on {thread['path']}:{thread['line']}" if thread.get("path") else ""
    prompt = _REFINE_PROMPT.format(
        number=number, title=pr["title"], repo=config.repo(),
        author=thread["author"], where=where, body=thread["body"], note=note,
    )

    async with _ANALYSIS_SEM:
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", prompt,
            "--allowedTools", "Bash(gh pr diff:*)", "Bash(gh pr view:*)",
            "Read", "Grep", "Glob",
            cwd=str(PROJECT_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=420)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {"ok": False, "error": "timed out refining the instruction"}

    if proc.returncode != 0:
        return {"ok": False, "error": (stderr.decode(errors="replace") or "").strip()}

    out = stdout.decode(errors="replace")
    instruction = _block(out, "INSTRUCTION")
    if not instruction:
        return {"ok": False, "error": "no <INSTRUCTION> block in output"}
    reply_draft = _block(out, "REPLY")
    notes = _block(out, "NOTES")

    with db.conn() as c:
        c.execute(
            """INSERT INTO thread_refinements
                 (pr_number, thread_id, note, instruction, reply_draft)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(pr_number, thread_id) DO UPDATE SET
                 note=excluded.note, instruction=excluded.instruction,
                 reply_draft=excluded.reply_draft, handoff_path=NULL,
                 created_at=datetime('now')""",
            (number, str(thread_id), note, instruction, reply_draft),
        )
    db.log_action(number, "thread_refined", f"thread {thread_id}")
    return {
        "ok": True, "instruction": instruction,
        "reply_draft": reply_draft, "notes": notes,
    }


def write_handoff(number, thread_id):
    """Write the refined instruction to a file a Claude Code session can read.

    Deliberately outside the target checkout: dropping files into the repo would
    show up in `git status` and risk being committed. The user pastes the path
    into a session running in their checkout instead.
    """
    with db.conn() as c:
        row = c.execute(
            "SELECT * FROM thread_refinements WHERE pr_number=? AND thread_id=?",
            (number, str(thread_id)),
        ).fetchone()
    if row is None or not row["instruction"]:
        return {"ok": False, "error": "nothing refined for this thread yet"}

    HANDOFF_DIR.mkdir(parents=True, exist_ok=True)
    path = HANDOFF_DIR / f"pr-{number}-thread-{thread_id}.md"
    path.write_text(
        f"# PR #{number} — feedback to act on\n\n"
        f"Repo: {config.repo()}\n"
        f"PR: https://github.com/{config.repo()}/pull/{number}\n\n"
        f"## What the author decided\n\n{row['note']}\n\n"
        f"## Instruction\n\n{row['instruction']}\n"
    )
    with db.conn() as c:
        c.execute(
            "UPDATE thread_refinements SET handoff_path=? "
            "WHERE pr_number=? AND thread_id=?",
            (str(path), number, str(thread_id)),
        )
    return {"ok": True, "path": str(path)}

