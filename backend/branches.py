"""Work that exists as a branch but was never raised as a PR.

The My PRs tab only ever looked at open PRs, so a branch that was pushed and
forgotten, or whose PR was closed without merging, was invisible — the question
"where did that work go" had no answer in the tool.

Read from the local checkout rather than GitHub: it is instant, it sees
branches that were never pushed at all, and it can say how far each has drifted
from trunk without a call per branch. The cost is that it is only as current as
the last fetch, so the age of that is reported rather than hidden.
"""
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import config, db

# Most of these branches are months old. A window keeps the list to what is
# plausibly still live; on a real checkout 30 days gave 2 branches where 60
# gave 35, which is the difference between a list and a wall.
DEFAULT_DAYS = 30


def _repo_dir():
    d = config.target_repo_dir()
    return Path(d) if d else None


def _git(args, cwd, check=False):
    r = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True
    )
    if check and r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "").strip()[:300])
    return r.stdout.strip()


def _trunk(cwd):
    ref = _git(["rev-parse", "--abbrev-ref", "origin/HEAD"], cwd)
    return ref or "origin/trunk"


def fetch_age_hours(cwd):
    """How stale the local view of the remote is, in hours."""
    f = Path(cwd) / ".git" / "FETCH_HEAD"
    if not f.exists():
        return None
    age = datetime.now(timezone.utc).timestamp() - f.stat().st_mtime
    return round(age / 3600, 1)


def fetch(cwd=None):
    cwd = cwd or _repo_dir()
    if cwd is None:
        return {"ok": False, "error": "no target_repo_dir configured"}
    r = subprocess.run(
        ["git", "-C", str(cwd), "fetch", "--prune", "origin"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return {"ok": False, "error": (r.stderr or "").strip()[:300]}
    return {"ok": True, "fetched_hours_ago": 0}


def _open_pr_branches():
    r = subprocess.run(
        ["gh", "pr", "list", "--repo", config.repo(), "--author", config.self_login(),
         "--state", "open", "--limit", "100", "--json", "headRefName"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return set()
    try:
        return {p["headRefName"] for p in json.loads(r.stdout or "[]")}
    except ValueError:
        return set()


def list_unraised(days=DEFAULT_DAYS):
    """Local branches carrying unmerged work with no open PR."""
    cwd = _repo_dir()
    if cwd is None or not (cwd / ".git").exists():
        return {"branches": [], "error": "no usable target_repo_dir configured"}

    trunk = _trunk(cwd)
    open_branches = _open_pr_branches()
    with db.conn() as c:
        notes = {r["branch"]: dict(r) for r in c.execute("SELECT * FROM branch_notes")}

    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    raw = _git([
        "for-each-ref", "--format=%(refname:short)\t%(committerdate:unix)",
        "refs/heads",
    ], cwd)

    out = []
    for line in raw.splitlines():
        if "\t" not in line:
            continue
        name, ts = line.rsplit("\t", 1)
        if name in open_branches:
            continue
        note = notes.get(name)
        if note and note.get("status") == "dismissed":
            continue
        try:
            when = int(ts)
        except ValueError:
            continue
        if days and when < cutoff:
            continue
        # `git cherry` marks commits already applied upstream with '-', which
        # catches squash-merged branches that rev-list still counts as ahead.
        cherry = _git(["cherry", trunk, name], cwd)
        unmerged = [l for l in cherry.splitlines() if l.startswith("+")]
        if not unmerged:
            continue
        behind = _git(["rev-list", "--count", f"{name}..{trunk}"], cwd) or "0"
        subject = _git(["log", "-1", "--format=%s", name], cwd)
        on_remote = bool(_git(["ls-remote", "--heads", "origin", name], cwd))
        m = re.match(r"(?i)([a-z]+-\d+)", name.split("/", 1)[0])
        out.append({
            "branch": name,
            "ticket": m.group(1).upper() if m else None,
            "ahead": len(unmerged),
            "behind": int(behind),
            "last_commit_at": datetime.fromtimestamp(when, timezone.utc).isoformat(),
            "last_subject": subject,
            "on_remote": on_remote,
            "note": note,
        })
    out.sort(key=lambda b: b["last_commit_at"], reverse=True)
    return {
        "branches": out,
        "fetched_hours_ago": fetch_age_hours(cwd),
        "trunk": trunk,
        "days": days,
    }


def unmerged_shas(branch, trunk, cwd):
    """Commits on the branch that are not upstream in any form.

    `git cherry` marks a commit '-' when an equivalent patch is already on
    trunk, which is how a squash-merged branch is recognised. Diffing
    `trunk...branch` on such a branch shows the whole merged feature again, so
    anything summarising "what is outstanding here" has to work from these.
    """
    out = []
    for line in _git(["cherry", trunk, branch], cwd).splitlines():
        if line.startswith("+"):
            out.append(line.split()[1])
    return out


def diff_summary(branch, trunk=None, max_chars=12000):
    """The branch's unmerged commits and their diff, for summarising.

    Deliberately scoped to the unmerged commits. A branch whose PR was
    squash-merged still looks 'ahead' of trunk, and diffing the whole branch
    described a leftover one-commit refactor as though it were the entire
    feature that had already shipped.
    """
    cwd = _repo_dir()
    trunk = trunk or _trunk(cwd)
    shas = unmerged_shas(branch, trunk, cwd)
    if not shas:
        return "", "", ""
    commits = _git(
        ["log", "--no-walk", "--format=%h %s", *reversed(shas)], cwd
    )
    span = f"{shas[0]}^..{branch}"
    stat = _git(["diff", "--stat", span], cwd)
    patch = _git(["diff", span], cwd)
    if len(patch) > max_chars:
        patch = patch[:max_chars] + "\n… (diff truncated)"
    return commits, stat, patch


def catch_up_command(branch, trunk):
    """What to paste to bring a drifted branch back onto trunk."""
    base = (trunk or "origin/trunk").replace("origin/", "")
    return (
        f"git fetch origin && git checkout {branch} && "
        f"git rebase origin/{base}"
    )


def raise_pr(branch, title, body, draft=False):
    """Push the branch if it is local-only, then open a PR from it."""
    cwd = _repo_dir()
    if cwd is None:
        return {"ok": False, "error": "no target_repo_dir configured"}
    if not _git(["ls-remote", "--heads", "origin", branch], cwd):
        r = subprocess.run(
            ["git", "-C", str(cwd), "push", "-u", "origin", branch],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            return {"ok": False, "error": "push failed: " + (r.stderr or "").strip()[:300]}
    cmd = [
        "gh", "pr", "create", "--repo", config.repo(), "--head", branch,
        "--title", title, "--body", body,
    ]
    if draft:
        cmd.append("--draft")
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=str(cwd))
    if r.returncode != 0:
        return {"ok": False, "error": (r.stderr or r.stdout or "").strip()[:400]}
    url = (r.stdout or "").strip().splitlines()[-1] if r.stdout else ""
    with db.conn() as c:
        c.execute(
            "UPDATE branch_notes SET status='raised' WHERE branch=?", (branch,)
        )
    db.log_action(None, "branch_pr_raised", f"{branch} -> {url}")
    return {"ok": True, "url": url}


def dismiss(branch):
    with db.conn() as c:
        c.execute(
            """INSERT INTO branch_notes (branch, status) VALUES (?, 'dismissed')
               ON CONFLICT(branch) DO UPDATE SET status='dismissed'""",
            (branch,),
        )
    return {"ok": True}

_SUMMARY_PROMPT = """This is a branch in `{repo}` that was never raised as a
pull request. Work out what is on it and whether it is worth raising.

**Ignore any skills or CLAUDE.md files in scope.** They are not part of this task.

Branch `{branch}`{ticket}, last touched {age} days ago. {ahead} commit(s) of its
own, {behind} commits behind trunk.

# Its commits

{commits}

# What it changes

{stat}

# The diff

{patch}

# Who is reading this

The person who wrote it, weeks later, who does not remember. Plain English, no
identifiers or file paths in the prose. They want to know in one read whether
this is worth reviving or should be dropped.

# What to produce

`summary` — two or three sentences on what this branch actually does and why
someone started it. Say what changes for a user, or say plainly that nothing
does because it is a refactor or a test.

`state` — whether it looks finished. Say what is missing if anything obvious is:
no tests, a TODO left in, a half-written method, a migration with no model
change. If it looks complete, say so. If you cannot tell, say that rather than
guessing.

`advice` — one sentence. Raise it, rebase it first, fold it into something else,
or drop it. {behind} commits of drift is the main thing to weigh: a small change
that far behind may be faster to redo than to rebase, and if the work has since
been done another way it is already dead.

`pr_title` — a title in this team's style: `[TICKET] Sentence case description`,
using the ticket above when there is one. No trailing full stop.

`pr_body` — the PR description. Open with a plain-English line saying where this
sits and what changes for anyone today, then what it does, then anything a
reviewer should know. Markdown, short.

# Output

<ANSWER>
{{"summary": "...", "state": "...", "advice": "...",
  "pr_title": "...", "pr_body": "..."}}
</ANSWER>"""


async def summarise(branch):
    """Work out what an unraised branch contains, and draft its PR."""
    import asyncio
    from datetime import datetime, timezone as _tz

    cwd = _repo_dir()
    if cwd is None:
        return {"ok": False, "error": "no target_repo_dir configured"}
    listing = list_unraised(days=0)
    meta = next((b for b in listing["branches"] if b["branch"] == branch), None)
    if meta is None:
        return {"ok": False, "error": "that branch has no unmerged work, or is already raised"}

    trunk = listing["trunk"]
    commits, stat, patch = diff_summary(branch, trunk)
    age = max(0, int(
        (datetime.now(_tz.utc) - datetime.fromisoformat(meta["last_commit_at"])).days
    ))
    prompt = _SUMMARY_PROMPT.format(
        repo=config.repo(), branch=branch,
        ticket=f" (ticket {meta['ticket']})" if meta["ticket"] else "",
        age=age, ahead=meta["ahead"], behind=meta["behind"],
        commits=commits or "(none)", stat=stat or "(no changes)",
        patch=patch or "(empty diff)",
    )

    proc = await asyncio.create_subprocess_exec(
        "claude", "-p", prompt,
        "--allowedTools", "Read", "Grep", "Glob",
        cwd=str(Path(__file__).resolve().parent.parent),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=420)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {"ok": False, "error": "timed out reading the branch"}

    from .my_prs import claude_error, _block
    if proc.returncode != 0:
        return {"ok": False, "error": claude_error(stdout, stderr, proc.returncode)}
    raw = _block(stdout.decode(errors="replace"), "ANSWER")
    if not raw:
        return {"ok": False, "error": "no <ANSWER> block in output"}
    try:
        data = json.loads(raw)
    except ValueError as e:
        return {"ok": False, "error": f"invalid JSON: {e}"}

    note = "\n\n".join(
        x for x in [data.get("state", ""), data.get("advice", "")] if x
    )
    with db.conn() as c:
        c.execute(
            """INSERT INTO branch_notes (branch, summary, state_note, pr_title, pr_body)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(branch) DO UPDATE SET
                 summary=excluded.summary, state_note=excluded.state_note,
                 pr_title=excluded.pr_title, pr_body=excluded.pr_body,
                 status='open', created_at=datetime('now')""",
            (branch, data.get("summary", ""), note,
             data.get("pr_title", ""), data.get("pr_body", "")),
        )
    return {"ok": True, **data}

def try_catch_up(branch):
    """Rebase a branch onto trunk in a scratch worktree, or explain why not.

    Done in a throwaway worktree so the user's own checkout is never touched:
    their current branch, their uncommitted work and their editor state all
    stay exactly as they were, whatever happens here. A rebase that conflicts
    is aborted and the worktree removed, so the repo is never left mid-rebase
    for someone who would not know how to get out of one.
    """
    cwd = _repo_dir()
    if cwd is None:
        return {"ok": False, "error": "no target_repo_dir configured"}
    trunk = _trunk(cwd)
    scratch = Path(cwd) / ".git" / "prw-catchup"
    subprocess.run(["git", "-C", str(cwd), "worktree", "remove", "--force",
                    str(scratch)], capture_output=True, text=True)

    r = subprocess.run(
        ["git", "-C", str(cwd), "worktree", "add", "--detach", str(scratch), branch],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return {"ok": False, "error": (r.stderr or "").strip()[:300]}

    try:
        before = _git(["rev-list", "--count", f"{branch}..{trunk}"], cwd) or "0"
        reb = subprocess.run(
            ["git", "-C", str(scratch), "rebase", trunk],
            capture_output=True, text=True,
        )
        if reb.returncode != 0:
            conflicts = _git(
                ["diff", "--name-only", "--diff-filter=U"], scratch
            ).splitlines()
            subprocess.run(["git", "-C", str(scratch), "rebase", "--abort"],
                           capture_output=True, text=True)
            return {
                "ok": False,
                "conflicted": True,
                "files": conflicts,
                "behind": int(before),
                "error": (
                    "This one cannot be caught up automatically — the same lines "
                    "have been changed on trunk since, so someone has to decide "
                    "which version wins."
                ),
            }

        new_sha = _git(["rev-parse", "HEAD"], scratch)
        current = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)

        if current == branch:
            # git refuses to move a branch that is checked out, so the rebase
            # has to happen in the checkout itself. The scratch run above has
            # already proved it applies cleanly, and a dirty tree is refused
            # rather than risking someone's uncommitted work.
            if _git(["status", "--porcelain"], cwd):
                return {
                    "ok": False,
                    "error": (
                        "This branch is the one you have open, and you have "
                        "unsaved changes in it. It does rebase cleanly — commit "
                        "or stash what you are working on and press this again."
                    ),
                }
            reb2 = subprocess.run(
                ["git", "-C", str(cwd), "rebase", trunk],
                capture_output=True, text=True,
            )
            if reb2.returncode != 0:
                subprocess.run(["git", "-C", str(cwd), "rebase", "--abort"],
                               capture_output=True, text=True)
                return {"ok": False, "error": (reb2.stderr or "").strip()[:300]}
        else:
            mv = subprocess.run(
                ["git", "-C", str(cwd), "branch", "-f", branch, new_sha],
                capture_output=True, text=True,
            )
            if mv.returncode != 0:
                return {"ok": False, "error": (mv.stderr or "").strip()[:300]}
        ahead = _git(["rev-list", "--count", f"{trunk}..{branch}"], cwd) or "0"
        db.log_action(None, "branch_caught_up", f"{branch} (+{before} behind cleared)")
        return {
            "ok": True,
            "was_behind": int(before),
            "ahead": int(ahead),
        }
    finally:
        subprocess.run(["git", "-C", str(cwd), "worktree", "remove", "--force",
                        str(scratch)], capture_output=True, text=True)


def working_tree_dirty():
    cwd = _repo_dir()
    if cwd is None:
        return False
    return bool(_git(["status", "--porcelain"], cwd))

