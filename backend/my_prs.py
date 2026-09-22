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
import re
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


def claude_error(stdout, stderr, returncode):
    """Explain why a `claude -p` call failed.

    The CLI reports some fatal conditions on stdout rather than stderr — an
    expired OAuth session is the common one — so reading stderr alone produces
    an empty error and a UI that says nothing went wrong. Prefer whichever
    stream actually carries a message, and name the fix when we recognise it.
    """
    out = (stdout or b"").decode(errors="replace").strip()
    err = (stderr or b"").decode(errors="replace").strip()
    # The CLI warns about stdin on every headless call; it is never the cause.
    err = "\n".join(
        l for l in err.splitlines() if "no stdin data received" not in l
    ).strip()
    msg = err or out or f"claude exited with code {returncode}"
    low = msg.lower()
    if "oauth" in low or "authenticate" in low or "not logged in" in low:
        return ("Claude is signed out. Run `claude auth login` in a terminal, "
                f"then try again. ({msg.splitlines()[0][:160]})")
    return msg[:600]


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
    # CANCELLED is deliberately not a failure. It almost always means a newer
    # run superseded this one, and `gh pr checks` ignores them too — counting
    # them marked a PR whose checks had all passed as "build failing".
    if any(s in ("FAILURE", "ERROR", "TIMED_OUT", "ACTION_REQUIRED") for s in states):
        return "red"
    if any(s in ("PENDING", "IN_PROGRESS", "QUEUED", "EXPECTED", "") for s in states):
        return "pending"
    return "green"


# Paths that mean this PR carries something the author must not run themselves.
# Matched on exact directory prefixes and filename parts rather than loose
# keywords: "task" appears in app/policies/tasks/, and a warning that fires on
# ordinary code is a warning people learn to scroll past.
def _risk_flags(files):
    """Operational risks in a PR that change who should be pressing the button."""
    paths = [f.get("path", "") for f in files or []]
    backfills = [
        p for p in paths
        if (p.startswith("lib/one_off/") or p.startswith("lib/manual_one_off/")
            or "backfill" in p.rsplit("/", 1)[-1].lower())
        and not p.startswith("test/")
    ]
    migrations = [p for p in paths if p.startswith("db/migrate/")]
    flags = []
    if backfills:
        flags.append({
            "level": "major",
            "label": "Do not run this yourself",
            "detail": (
                "This adds a data backfill. It needs an engineer to run it, on a "
                "dry run first and coordinated with CALM — a bundled backfill "
                "erroring on deploy has blocked the release train before. Raise "
                "it as its own PR and hand it over rather than running it."
            ),
            "files": backfills,
        })
    if migrations:
        flags.append({
            "level": "major" if backfills else "warn",
            "label": "Changes the database schema",
            "detail": (
                "Migrations here need a dev to run and Strong Migrations will not "
                "catch everything. Reverting the PR does not undo a migration that "
                "has already run."
            ),
            "files": migrations,
        })
    return flags


def _failing_checks(rollup):
    """The checks actually standing in the way, with a link and their run id.

    The run id is what `gh run rerun` needs, and it is only available by
    parsing the job URL — the rollup does not carry it directly.
    """
    out = []
    for c in rollup or []:
        state = (c.get("conclusion") or c.get("state") or "").upper()
        if state not in ("FAILURE", "ERROR", "TIMED_OUT", "ACTION_REQUIRED"):
            continue
        link = c.get("detailsUrl") or c.get("targetUrl") or ""
        m = re.search(r"/actions/runs/(\d+)", link)
        out.append({
            "name": c.get("name") or c.get("context") or "unnamed check",
            "state": state,
            "link": link,
            "run_id": m.group(1) if m else None,
        })
    return out


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

    # Whether anyone has actually engaged with this PR, and when the author
    # last spoke. This is what separates "nobody has looked at it" from "they
    # looked, I answered, and now I am waiting" — two situations that need
    # opposite actions but look identical from the outstanding-thread count.
    others = [
        c for c in inline + issues
        if c["user"]["login"] != self_login
        and not _is_noise(c["user"]["login"], c.get("body"))
    ]
    mine = [c for c in inline + issues if c["user"]["login"] == self_login]
    return {
        "threads": out,
        "engaged": bool(others),
        "last_self_comment_at": max((c["created_at"] for c in mine), default=None),
    }


def _open_threads(threads):
    """Threads still needing a decision from the author.

    A thread stays outstanding on GitHub until somebody replies there, but the
    author may have already dealt with it here — skipped it, answered it, or
    handed the work to Claude. Counting those as still waiting left a PR
    reporting comments to decide with an empty list underneath and no way to
    move it on.
    """
    out = []
    for t in threads:
        a = t.get("analysis")
        if a is None or a.get("status") in ("pending", "handed_off"):
            out.append(t)
    return out


def _categorise(pr, checks, threads, engaged):
    if pr["is_draft"] or checks == "red":
        return "not_ready"
    if _open_threads(threads):
        return "comments"
    if pr["review_decision"] == "APPROVED" and checks in ("green", "none"):
        return "ready"
    # Nothing outstanding and somebody has already been through it: the ball is
    # with them, not the author. Calling this "push for a review" would tell the
    # author to chase a reviewer who is mid-review.
    if engaged:
        return "waiting"
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
            "--json",
            "number,statusCheckRollup,updatedAt,additions,deletions,files",
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
        handovers = {
            r["pr_number"]: dict(r)
            for r in c.execute("SELECT * FROM handovers").fetchall()
        }
        bundles = {
            r["pr_number"]: dict(r)
            for r in c.execute("SELECT * FROM pr_bundles").fetchall()
        }

    out = []
    for p in prs:
        d = detail.get(p["number"], {})
        checks = _checks_state(d.get("statusCheckRollup"))
        try:
            info = _threads(p["number"], self_login)
        except Exception as e:  # noqa: BLE001 - one bad PR must not blank the tab
            info = {"threads": [], "engaged": False, "last_self_comment_at": None}
            p["threads_error"] = str(e)
        threads = info["threads"]
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
            "category": _categorise(p, checks, threads, info["engaged"]),
            "open_count": len(_open_threads(threads)),
            "failing_checks": _failing_checks(d.get("statusCheckRollup")),
            "risks": _risk_flags(d.get("files")),
            "handover": handovers.get(p["number"]),
            "waiting_since": info["last_self_comment_at"],
            "review_request": requests.get(p["number"]),
            "bundle": bundles.get(p["number"]),
            "decided_count": sum(
                1 for t in threads
                if t.get("refinement") and t["refinement"].get("instruction")
            ),
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

# Who you are disagreeing with

These reviewers are senior engineers on this codebase. They have context you do
not: how this area behaves in production, what was tried before, what the team
agreed last month. You are reading a diff. They are not always right, but the
prior is that they raised it for a reason.

So disagreement has to be earned:

- Before you argue, work out what a competent person would have been thinking.
  If you cannot construct that, you have not understood the comment yet.
- Then name the specific thing they would have had to not know for your
  position to hold — a file they cannot see from the diff, a convention
  elsewhere in the codebase, a constraint from another PR. **If you cannot name
  it, they are probably right and you should say so.** "I think this is fine"
  is not a reason.
- Never argue from the mere absence of a problem. "Nothing breaks today" is an
  observation, not a case.
- Pushing back is not free. It costs the author a round trip, and if the
  reviewer turns out to be right it reads as dodging the work. Recommend it
  only when you would still recommend it knowing that.

For each comment, decide what it actually needs:

- `code_fix` — they are right and the code should change. This is the correct
  answer more often than it feels, especially for anything about where code
  lives, naming, or test coverage.
- `reply` — no code change needed, but it deserves an answer, and you can name
  what they would have had to not know.
- `unsure` — you cannot tell from here. The answer turns on something you
  cannot check: how it behaves at scale, what was agreed previously, whether a
  convention holds elsewhere. Say what you would need to know. This is a real
  answer, not a failure, and it is better than a confident wrong one.
- `no_action` — informational, already handled, or resolved by a later commit.

Then write, for each:

- `headline` — at most ten words, naming what this comment is about. It is
  read on a collapsed row with a dozen others, so it has to work alone. Say the
  subject, not the verdict: "Where the permission rule lives", not "Reviewer
  disagrees". No identifiers, no file names.
- `wants` — an array of one to three strings, each under fifteen words, saying
  what the commenter is asking for. One ask per entry. These are read before
  anything else and often instead of everything else, so they carry the
  substance, not a trailer for it. Not full sentences, no trailing full stops.
- `summary` — what the commenter is actually asking, in plain English, for
  someone who read the bullets and wants the rest. Two or three sentences. No
  identifiers, paths or line numbers in this field. The reader is not an
  engineer. Say what it means for the change, not what the code says.
- `their_case` — one sentence, under twenty-five words, on why a reviewer who
  knows this codebase would raise this. Written straight, not as a strawman you
  are about to knock over. Required on every comment, including the ones you
  agree with.
- `unknowns` — one sentence naming what you could not check for yourself and
  would change the answer, or an empty string when the diff really does settle
  it. Do not pad this; an empty string is fine when it is true.
- `recommendation` — two or three sentences. It must open with the exact
  phrase for its action, so the opener and the badge never disagree and the
  reader learns the four of them:

  | action | opening phrase |
  |---|---|
  | `code_fix` | `Follow their suggestion.` |
  | `reply` | `Explain, don't change it.` |
  | `unsure` | `I can't tell from here.` |
  | `no_action` | `Nothing to do.` |

  Then the reason, in the same sentence or the next. Where you disagree, the
  reason must name the thing they would have had to not know, not merely
  restate your preference. Where you agree, say what specifically convinced
  you, so the author can check the judgement rather than take it on trust.

  Never open with a bare pronoun — "Take it", "Do it", "Leave it" — which reads
  as a verdict handed down rather than a recommendation with a reason behind
  it.
- `reply_draft` — the message to send, written as the PR author speaking to the
  commenter. It gets read on a phone between meetings, so structure it:

  1. One line saying where you land. Not a preamble, the actual position.
  2. Two to four markdown bullets carrying the reasons, one point each. Short
     enough to scan. This is the part people actually read.
  3. Where you are disagreeing, a line starting "What would change my mind:"
     naming what would flip you. This makes the disagreement checkable instead
     of a matter of taste, and it hands them the fastest route to settling it.
  4. Where relevant, the offer — what you will do if they still disagree.

  Direct and courteous. No throat-clearing, no apologising for existing, no
  thanking them twice. For `code_fix`, the same shape but shorter: what you are
  changing and anything you are deliberately not changing. For `unsure`, ask
  the actual question. Empty string for `no_action`.
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
[{{"thread_id": "...", "action": "reply",
   "headline": "Where the permission rule lives",
   "wants": ["Move it out of the shared file", "Scope it to this feature"],
   "their_case": "A per-feature flag on the class every controller inherits is a smell they have seen spread before",
   "unknowns": "Whether the team has agreed a home for feature gates elsewhere",
   "summary": "...", "recommendation": "...",
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
        return {"ok": False, "error": claude_error(stdout, stderr, proc.returncode)}

    out = stdout.decode(errors="replace")
    if "<ACTIONS>" not in out:
        return {"ok": False, "error": "no <ACTIONS> block in output"}
    raw = out.split("<ACTIONS>", 1)[1].split("</ACTIONS>", 1)[0].strip()
    try:
        items = json.loads(raw)
    except ValueError as e:
        return {"ok": False, "error": f"invalid JSON: {e}"}
    return {"ok": True, "items": items}


async def analyse(number, thread_id=None, only_missing=False):
    """Triage one of the user's own PRs and store the result.

    `thread_id` triages a single comment, and `only_missing` the ones a previous
    pass returned nothing for. A run over a dozen comments can quietly come back
    with eleven, and the twelfth was then stuck: no summary, no buttons, and the
    PR-level button hidden because some threads did have an analysis.
    """
    prs = {p["number"]: p for p in gather(config.self_login())}
    pr = prs.get(number)
    if pr is None:
        return {"ok": False, "error": "not one of your open PRs"}

    threads = pr["threads"]
    if thread_id is not None:
        threads = [t for t in threads if str(t["root_id"]) == str(thread_id)]
        if not threads:
            return {"ok": False, "error": "that comment is no longer outstanding"}
    elif only_missing:
        threads = [t for t in threads if not t.get("analysis")]
    if not threads:
        return {"ok": False, "error": "nothing left to work out on this PR"}

    res = await triage(number, pr["title"], threads)
    if not res["ok"]:
        return res
    items = res["items"]

    with db.conn() as c:
        for it in items:
            c.execute(
                """INSERT INTO my_pr_actions
                     (pr_number, thread_id, action, headline, wants, their_case,
                      unknowns, summary, recommendation, reply_draft, fix_prompt,
                      confidence, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                   ON CONFLICT(pr_number, thread_id) DO UPDATE SET
                     action=excluded.action, headline=excluded.headline,
                     wants=excluded.wants, their_case=excluded.their_case,
                     unknowns=excluded.unknowns, summary=excluded.summary,
                     recommendation=excluded.recommendation,
                     reply_draft=excluded.reply_draft,
                     fix_prompt=excluded.fix_prompt,
                     confidence=excluded.confidence,
                     status='pending', created_at=datetime('now')""",
                (
                    number, str(it.get("thread_id")), it.get("action", "reply"),
                    it.get("headline", ""),
                    json.dumps(it.get("wants") or []),
                    it.get("their_case", ""), it.get("unknowns", ""),
                    it.get("summary", ""), it.get("recommendation", ""),
                    it.get("reply_draft", ""), it.get("fix_prompt", ""),
                    it.get("confidence", "medium"),
                ),
            )
    db.log_action(number, "my_pr_triaged", f"{len(items)} of {len(threads)} threads")
    # A pass that silently returns fewer items than it was given is the failure
    # that stranded a comment, so say so rather than reporting success.
    return {
        "ok": True,
        "count": len(items),
        "asked": len(threads),
        "missed": max(0, len(threads) - len(items)),
    }

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
        return {"ok": False, "error": claude_error(stdout, stderr, proc.returncode)}

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
        return {"ok": False, "error": claude_error(stdout, stderr, proc.returncode)}

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

_BUNDLE_PROMPT = """The author of PR #{number} ("{title}") in `{repo}` has gone
through the outstanding comments and decided what to do about each. Merge those
decisions into one instruction and one reply.

**Ignore any skills or CLAUDE.md files in scope.** They are not part of this task.

Read the PR so the merged instruction is grounded in the real code:

```bash
gh pr diff {number} --repo {repo}
gh pr view {number} --repo {repo} --json title,body
```

# The decisions, one per comment

{blocks}

# Merging the instruction

One session will do all of this in one pass, so it needs to read as one job
rather than {count} stapled together.

- Put the work in the order it has to happen. If one change moves a file another
  change edits, the move comes first and the second refers to the new location.
- Fold genuine overlap together. Two comments asking for coverage of the same
  method is one instruction, not two.
- Never drop a decision to make the merge tidy. Every accepted item survives.
- Keep every out-of-scope line from the individual decisions. That is the part
  most likely to be lost in a merge and the part that matters most: those are
  the things the author explicitly refused.
- Verification steps get merged too — one command list at the end, not one per
  section.
- If two decisions genuinely conflict, do not pick a winner. Implement neither,
  and say so in `notes`.

# Merging the reply

One comment on the PR conversation, addressed to everyone who commented.

- Group by decision, not by commenter. What is being done, then what is not.
- Attribute where it matters — if a specific reviewer raised something you are
  declining, they should be able to see your reason without hunting.
- Do not thank everyone individually and do not open with a summary of the PR.
- Keep the refusals as clear as they were individually. A merged reply that
  softens four "no"s into one vague paragraph is worse than four separate ones.
- Sentence case, British English, no em-dashes.

# Output

<INSTRUCTION>
the merged instruction
</INSTRUCTION>

<REPLY>
the single comment to post
</REPLY>

Anything the author should know — conflicts, something you could not reconcile,
a decision that turns out to be a bad idea next to another one. Omit the block
if there is nothing:

<NOTES>
...
</NOTES>"""


async def bundle(number):
    """Merge every decision made on one PR into one instruction and one reply."""
    prs = {p["number"]: p for p in gather(config.self_login())}
    pr = prs.get(number)
    if pr is None:
        return {"ok": False, "error": "not one of your open PRs"}

    decided, undecided = [], 0
    for t in pr["threads"]:
        ref = t.get("refinement")
        if ref and ref.get("instruction"):
            decided.append((t, ref))
        else:
            undecided += 1
    if not decided:
        return {"ok": False, "error": "nothing decided yet — refine a comment first"}

    blocks = []
    for i, (t, ref) in enumerate(decided, 1):
        where = f"{t['path']}:{t['line']}" if t.get("path") else "PR conversation"
        blocks.append(
            f"## Decision {i} — {t['author']} on {where}\n\n"
            f"### What they said\n\n{t['body']}\n\n"
            f"### What the author decided\n\n{ref['note']}\n\n"
            f"### The instruction written for it\n\n{ref['instruction']}\n\n"
            f"### The reply written for it\n\n{ref.get('reply_draft') or '(none)'}"
        )

    prompt = _BUNDLE_PROMPT.format(
        number=number, title=pr["title"], repo=config.repo(),
        blocks="\n\n---\n\n".join(blocks), count=len(decided),
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
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {"ok": False, "error": "timed out merging the decisions"}

    if proc.returncode != 0:
        return {"ok": False, "error": claude_error(stdout, stderr, proc.returncode)}

    out = stdout.decode(errors="replace")
    instruction = _block(out, "INSTRUCTION")
    if not instruction:
        return {"ok": False, "error": "no <INSTRUCTION> block in output"}
    reply = _block(out, "REPLY")
    notes = _block(out, "NOTES")
    covered = ",".join(str(t["root_id"]) for t, _ in decided)

    with db.conn() as c:
        c.execute(
            """INSERT INTO pr_bundles
                 (pr_number, instruction, reply, covered_threads, status)
               VALUES (?, ?, ?, ?, 'draft')
               ON CONFLICT(pr_number) DO UPDATE SET
                 instruction=excluded.instruction, reply=excluded.reply,
                 covered_threads=excluded.covered_threads, handoff_path=NULL,
                 status='draft', posted_at=NULL, created_at=datetime('now')""",
            (number, instruction, reply, covered),
        )
    db.log_action(number, "bundle_built", f"{len(decided)} decisions")
    return {
        "ok": True, "instruction": instruction, "reply": reply,
        "notes": notes, "covered": len(decided), "undecided": undecided,
    }


def write_bundle_handoff(number):
    """Write the merged instruction out for one Claude Code session to work from."""
    text = assemble_summary(number)
    if text is None:
        return {"ok": False, "error": "nothing decided on this PR yet"}

    HANDOFF_DIR.mkdir(parents=True, exist_ok=True)
    path = HANDOFF_DIR / f"pr-{number}-all-feedback.md"
    path.write_text(text + "\n")
    with db.conn() as c:
        c.execute(
            "UPDATE pr_bundles SET handoff_path=? WHERE pr_number=?",
            (str(path), number),
        )
    return {"ok": True, "path": str(path)}

def assemble_summary(number):
    """Collect the decisions already written on a PR into one document.

    Deliberately not an LLM call. The refinements are the work; stitching them
    together is string concatenation, and running them back through a model
    would add a wait and risk quietly restating what the author decided.
    """
    prs = {p["number"]: p for p in gather(config.self_login())}
    pr = prs.get(number)
    if pr is None:
        return None
    decided = [
        (t, t["refinement"]) for t in pr["threads"]
        if t.get("refinement") and t["refinement"].get("instruction")
    ]
    if not decided:
        return None

    lines = [
        f"# PR #{number} — {pr['title']}",
        "",
        f"Repo: {config.repo()}",
        f"PR: {pr['url']}",
        "",
        f"{len(decided)} of {len(pr['threads'])} comments decided.",
        "",
    ]
    for i, (t, ref) in enumerate(decided, 1):
        where = f"{t['path']}:{t['line']}" if t.get("path") else "PR conversation"
        lines += [
            "---",
            "",
            f"## {i}. {t['author']} on {where}",
            "",
            "**What they said**",
            "",
            t["body"].strip(),
            "",
            "**What I decided**",
            "",
            ref["note"].strip(),
            "",
            "**Instruction**",
            "",
            ref["instruction"].strip(),
            "",
        ]
        if ref.get("reply_draft"):
            lines += ["**Reply drafted for them**", "", ref["reply_draft"].strip(), ""]

    undecided = [t for t in pr["threads"] if not (
        t.get("refinement") and t["refinement"].get("instruction")
    )]
    if undecided:
        lines += [
            "---",
            "",
            "## Not decided yet",
            "",
        ]
        for t in undecided:
            where = f"{t['path']}:{t['line']}" if t.get("path") else "PR conversation"
            lines.append(f"- {t['author']} on {where}")
        lines.append("")
    return "\n".join(lines)

def rerun_failed_checks(number):
    """Re-run the failed jobs on this PR's most recent workflow runs."""
    detail = _gh_json([
        "gh", "pr", "list", "--repo", config.repo(), "--state", "open",
        "--limit", "50", "--json", "number,statusCheckRollup",
    ])
    rollup = next(
        (p.get("statusCheckRollup") for p in detail if p["number"] == number), None
    )
    runs = {c["run_id"] for c in _failing_checks(rollup) if c["run_id"]}
    if not runs:
        return {"ok": False, "error": "no failed workflow run to re-run"}
    failures = []
    for run_id in sorted(runs):
        r = subprocess.run(
            ["gh", "run", "rerun", run_id, "--failed", "--repo", config.repo()],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            failures.append(f"{run_id}: {(r.stderr or r.stdout or '').strip()[:200]}")
    if failures:
        return {"ok": False, "error": "; ".join(failures)}
    db.log_action(number, "checks_rerun", f"{len(runs)} run(s)")
    return {"ok": True, "runs": len(runs)}


def mark_ready_for_review(number):
    """Take one of the user's own PRs out of draft."""
    r = subprocess.run(
        ["gh", "pr", "ready", str(number), "--repo", config.repo()],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return {"ok": False, "error": (r.stderr or r.stdout or "").strip()[:300]}
    db.log_action(number, "marked_ready", "")
    return {"ok": True}

_HANDOVER_PROMPT = """PR #{number} ("{title}") in `{repo}` contains work its
author must not run themselves. Write the handover.

**Ignore any skills or CLAUDE.md files in scope.** They are not part of this task.

Read it properly before writing anything — the ordering is the whole point and
you cannot get it from the file names:

```bash
gh pr diff {number} --repo {repo}
gh pr view {number} --repo {repo} --json title,body,files
```

Read the migrations and the one-off scripts themselves. Look at what each one
touches, whether one depends on another having run, and whether anything is
gated on a feature flag.

# What was flagged

{risks}

# Who is reading this

The runbook goes to an engineer who did not write this code. They need to run
it without reconstructing the author's reasoning, and without discovering a
prerequisite halfway through.

# The runbook

Markdown. In this order, and skip a heading only when it genuinely does not
apply:

**Before you start** — what must already be true. Deploys that must have
landed, migrations that must have run, flags that must be off, and explicitly
whether anything here can run before something else. If two things must happen
in a set order, number them and say what breaks if they are swapped. If order
does not matter, say that too — the reader will otherwise assume it does.

**The order to run things** — numbered. One step per command. Real commands
where the diff tells you what they are, and say when you are inferring one. Say
which pods or environments, and whether it is per-pod.

**Dry run first** — how to run it without writing, and what the output should
look like if it is safe to proceed. If a script has no dry-run mode, say so
plainly; that is the thing the reader most needs to know up front.

**What to check afterwards** — the specific thing that proves it worked, not
"verify it succeeded". A count that should match, a record that should now
exist, a page that should load.

**If it goes wrong** — whether it is re-runnable, whether a partial run leaves
a mess, and what to do about it. If reverting the PR does not undo the data
change, say that in as many words.

**What the author is not doing** — one line making clear they are handing this
over rather than having forgotten it.

# The message

Three or four sentences for a team channel. What the PR does, what is being
asked of them, what they need to have ready, and the link. No preamble, no
thanking them in advance. Plain English: someone scrolling past should be able
to tell whether it is their problem.

# The ticket

Say what should change on the Jira ticket so ownership actually moves —
assignee, status, and the comment worth leaving. One or two sentences. Never
claim to have done it.

# Output

<RUNBOOK>
...
</RUNBOOK>

<MESSAGE>
...
</MESSAGE>

<TICKET>
...
</TICKET>"""


def ticket_key_from(title):
    """The Jira key a Learn Amp PR title carries, e.g. [LA-40640]."""
    m = re.search(r"\[([A-Z][A-Z0-9]+-\d+)\]", title or "")
    return m.group(1) if m else None


def start_handover(number):
    """Mark a handover as running so the UI has something to show immediately.

    Writing the runbook takes minutes of model time. Holding the request open
    for that long gave a disabled button and nothing else, and a reload threw
    the work away — indistinguishable from a button that does nothing.
    """
    with db.conn() as c:
        row = c.execute(
            "SELECT status FROM handovers WHERE pr_number=?", (number,)
        ).fetchone()
        if row and row["status"] == "running":
            return {"ok": False, "error": "already working on this one"}
        c.execute(
            """INSERT INTO handovers (pr_number, status, started_at)
               VALUES (?, 'running', datetime('now'))
               ON CONFLICT(pr_number) DO UPDATE SET
                 status='running', started_at=datetime('now'), error=NULL""",
            (number,),
        )
    return {"ok": True}


def _fail_handover(number, error):
    with db.conn() as c:
        c.execute(
            "UPDATE handovers SET status='failed', error=? WHERE pr_number=?",
            (error[:500], number),
        )
    db.log_action(number, "handover_failed", error[:300])


async def handover(number):
    """Write the runbook, the ask, and the ticket change for a risky PR."""
    prs = {p["number"]: p for p in gather(config.self_login())}
    pr = prs.get(number)
    if pr is None:
        _fail_handover(number, "not one of your open PRs")
        return {"ok": False, "error": "not one of your open PRs"}
    risks = pr.get("risks") or []
    if not risks:
        _fail_handover(number, "nothing on this PR needs handing over")
        return {"ok": False, "error": "nothing on this PR needs handing over"}

    risk_text = "\n\n".join(
        f"- **{r['label']}** — {r['detail']}\n  Files: " + ", ".join(r["files"])
        for r in risks
    )
    prompt = _HANDOVER_PROMPT.format(
        number=number, title=pr["title"], repo=config.repo(), risks=risk_text
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
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            _fail_handover(number, "timed out writing the handover")
            return {"ok": False, "error": "timed out writing the handover"}

    if proc.returncode != 0:
        reason = claude_error(stdout, stderr, proc.returncode)
        _fail_handover(number, reason)
        return {"ok": False, "error": reason}

    out = stdout.decode(errors="replace")
    runbook = _block(out, "RUNBOOK")
    if not runbook:
        _fail_handover(number, "no <RUNBOOK> block in output")
        return {"ok": False, "error": "no <RUNBOOK> block in output"}
    message = _block(out, "MESSAGE")
    ticket_note = _block(out, "TICKET")
    key = ticket_key_from(pr["title"])

    with db.conn() as c:
        c.execute(
            """INSERT INTO handovers
                 (pr_number, runbook, message, ticket_key, ticket_note, status)
               VALUES (?, ?, ?, ?, ?, 'draft')
               ON CONFLICT(pr_number) DO UPDATE SET
                 runbook=excluded.runbook, message=excluded.message,
                 ticket_key=excluded.ticket_key, ticket_note=excluded.ticket_note,
                 status='draft', sent_at=NULL, created_at=datetime('now')""",
            (number, runbook, message, key, ticket_note),
        )
        c.execute("UPDATE handovers SET error=NULL WHERE pr_number=?", (number,))
    db.log_action(number, "handover_drafted", key or "")
    return {
        "ok": True, "runbook": runbook, "message": message,
        "ticket_key": key, "ticket_note": ticket_note,
    }


def send_handover(number, message):
    """Post the handover ask into the configured Teams channel."""
    hook = config.teams_webhook_url()
    if not hook:
        return {"ok": False, "error": "no Teams webhook configured"}
    r = subprocess.run(
        ["curl", "-sS", "-X", "POST", "-H", "Content-Type: application/json",
         "-d", json.dumps({"text": message}), "--max-time", "30",
         "-w", "\n%{http_code}", hook],
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
            "UPDATE handovers SET status='sent', sent_at=datetime('now') "
            "WHERE pr_number=?",
            (number,),
        )
    db.log_action(number, "handover_sent", config.teams_channel_label())
    return {"ok": True}

