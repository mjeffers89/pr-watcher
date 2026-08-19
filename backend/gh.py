"""Thin wrappers around the `gh` CLI. All I/O is JSON where possible.

The target repo comes from config.repo() so this module is repo-agnostic.
"""
import json
import re
import subprocess

from .config import repo as _repo


def _run(args, check=True):
    r = subprocess.run(args, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"gh failed: {' '.join(args)}\n{r.stderr}")
    return r.stdout.strip()


def list_open_prs():
    out = _run([
        "gh", "pr", "list", "--repo", _repo(), "--state", "open", "--limit", "50",
        "--json", "number,author,title,isDraft,headRefOid,url",
    ])
    return json.loads(out)


def list_my_open_prs(login):
    """Return the user's own open PRs with approval status.

    reviewDecision is GitHub's summary: APPROVED | CHANGES_REQUESTED |
    REVIEW_REQUIRED | "" (no reviewers assigned). approvals/changes_requested
    are raw counts across all submitted reviews for extra context.
    """
    out = _run([
        "gh", "pr", "list", "--repo", _repo(), "--author", login,
        "--state", "open", "--limit", "50",
        "--json", "number,title,url,isDraft,reviewDecision,reviews",
    ])
    prs = json.loads(out)
    result = []
    for p in prs:
        reviews = p.get("reviews") or []
        approvals = sum(1 for r in reviews if r.get("state") == "APPROVED")
        changes = sum(1 for r in reviews if r.get("state") == "CHANGES_REQUESTED")
        result.append({
            "number": p["number"],
            "title": p["title"],
            "url": p["url"],
            "is_draft": bool(p.get("isDraft")),
            "review_decision": p.get("reviewDecision") or "",
            "approvals": approvals,
            "changes_requested": changes,
        })
    result.sort(key=lambda p: p["number"], reverse=True)
    return result


def get_pr(number):
    out = _run([
        "gh", "pr", "view", str(number), "--repo", _repo(),
        "--json", "number,author,title,isDraft,headRefOid,url,body",
    ])
    return json.loads(out)


def pr_state(number):
    """Return 'OPEN' | 'CLOSED' | 'MERGED' for a PR (uppercase, matches gh)."""
    out = _run([
        "gh", "pr", "view", str(number), "--repo", _repo(),
        "--json", "state",
        "--jq", ".state",
    ])
    return (out or "").strip().upper()


def is_draft(number):
    """Return True if the PR is currently a draft."""
    out = _run([
        "gh", "pr", "view", str(number), "--repo", _repo(),
        "--json", "isDraft",
        "--jq", ".isDraft",
    ])
    return out.strip().lower() == "true"


def approvals_count(number):
    out = _run([
        "gh", "api", f"repos/{_repo()}/pulls/{number}/reviews",
        "--jq", "[.[] | select(.state == \"APPROVED\")] | length",
    ])
    return int(out or "0")


def self_reviews(number, login):
    out = _run([
        "gh", "api", f"repos/{_repo()}/pulls/{number}/reviews",
        "--jq", f"[.[] | select(.user.login == \"{login}\")] | length",
    ])
    return int(out or "0")


def has_human_review_activity(number, self_login, author_login=None):
    """True if any human OTHER than self and the PR author has reviewed/commented.

    Covers: PR-level reviews (COMMENTED, CHANGES_REQUESTED, APPROVED), inline
    review comments, and issue (PR thread) comments. Excludes self, the PR
    author (their own comments don't count as someone-else-reviewing), and
    any bot (type=Bot, login ending in '[bot]', or login starting with 'app/').
    """
    excluded = [self_login]
    if author_login:
        excluded.append(author_login)
    excluded_json = json.dumps(excluded)
    bot_filter = (
        f"(.user.login as $l | {excluded_json} | index($l) | not) "
        "and .user.type != \"Bot\" "
        "and (.user.login | endswith(\"[bot]\") | not) "
        "and (.user.login | startswith(\"app/\") | not)"
    )
    reviews = _run([
        "gh", "api", f"repos/{_repo()}/pulls/{number}/reviews",
        "--jq", f"[.[] | select({bot_filter})] | length",
    ])
    if int(reviews or "0") > 0:
        return True
    inline = _run([
        "gh", "api", "--paginate", f"repos/{_repo()}/pulls/{number}/comments",
        "--jq", f"[.[] | select({bot_filter})] | length",
    ])
    if int(inline or "0") > 0:
        return True
    issues = _run([
        "gh", "api", "--paginate", f"repos/{_repo()}/issues/{number}/comments",
        "--jq", f"[.[] | select({bot_filter})] | length",
    ])
    return int(issues or "0") > 0


def latest_commit_date(number):
    return _run([
        "gh", "api", f"repos/{_repo()}/pulls/{number}/commits",
        "--jq", ".[-1].commit.committer.date",
    ])


def latest_commit_sha(number):
    return _run([
        "gh", "api", f"repos/{_repo()}/pulls/{number}",
        "--jq", ".head.sha",
    ])


def issue_comments_from_others(number, self_login):
    out = _run([
        "gh", "api", f"repos/{_repo()}/issues/{number}/comments",
        "--jq", f"[.[] | select(.user.login != \"{self_login}\" and .user.type != \"Bot\")] | length",
    ])
    return int(out or "0")


def latest_issue_comment_at(number, self_login):
    """Max updated_at across issue comments written by anyone other than self.

    Returns ISO string or empty string if none.
    """
    return _run([
        "gh", "api", "--paginate", f"repos/{_repo()}/issues/{number}/comments",
        "--jq", (
            f"[.[] | select(.user.login != \"{self_login}\" "
            "and .user.type != \"Bot\") | .updated_at] | max // \"\""
        ),
    ])


def fetch_all_review_comments(number):
    """Return every inline review comment on the PR (needed to find reply threads)."""
    out = _run([
        "gh", "api", "--paginate", f"repos/{_repo()}/pulls/{number}/comments",
    ])
    # gh --paginate concatenates JSON arrays by stripping brackets; handle both.
    out = out.strip()
    if not out:
        return []
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        # Paginated output may be `[...][...]` — merge.
        parts = re.findall(r"\[.*?\](?=\s*\[|\s*$)", out, re.DOTALL)
        merged = []
        for p in parts:
            merged.extend(json.loads(p))
        return merged


def latest_review_comment_at(number, self_login):
    """Max updated_at across INLINE (pull-request review) comments from others.

    These are replies threaded under a specific line — where authors usually
    respond to review feedback.
    """
    return _run([
        "gh", "api", "--paginate", f"repos/{_repo()}/pulls/{number}/comments",
        "--jq", (
            f"[.[] | select(.user.login != \"{self_login}\" "
            "and .user.type != \"Bot\") | .updated_at] | max // \"\""
        ),
    ])


def diff_file_paths(number):
    """Return the set of file paths that are part of this PR's diff.

    Used to validate that a finding's `file` actually resolves to a changed
    file in the PR — GitHub's inline-comment endpoint returns 422
    "could not be resolved" if it doesn't, so we drop those findings up
    front instead of letting them rot as unpostable rows.
    """
    out = _run([
        "gh", "api", "--paginate", f"repos/{_repo()}/pulls/{number}/files",
        "--jq", ".[].filename",
    ])
    return {line for line in out.splitlines() if line}


def post_inline_comment(number, body, path, line, commit_sha):
    """Returns the created comment id."""
    r = subprocess.run(
        [
            "gh", "api", f"repos/{_repo()}/pulls/{number}/comments",
            "-f", f"body={body}",
            "-f", f"path={path}",
            "-F", f"line={line}",
            "-f", "side=RIGHT",
            "-f", f"commit_id={commit_sha}",
        ],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        # gh's stderr is just "gh: Validation Failed (HTTP 422)". The detailed
        # message ("line must be part of the diff", field-level errors, etc.)
        # lives in the response JSON, which gh prints to stdout on 4xx. Surface
        # both so the user can act on the actual cause.
        stderr_msg = (r.stderr or "").strip()
        body_msg = ""
        if r.stdout:
            try:
                payload = json.loads(r.stdout)
                pieces = []
                if payload.get("message"):
                    pieces.append(payload["message"])
                for err in payload.get("errors") or []:
                    if isinstance(err, dict):
                        # GitHub returns either {"message": "..."} or
                        # {"resource": "...", "field": "...", "code": "..."}
                        if err.get("message"):
                            pieces.append(err["message"])
                        else:
                            pieces.append(
                                f"{err.get('resource', '?')}.{err.get('field', '?')}: "
                                f"{err.get('code', '?')}"
                            )
                    else:
                        pieces.append(str(err))
                if payload.get("documentation_url"):
                    pieces.append(payload["documentation_url"])
                body_msg = " — " + " | ".join(pieces) if pieces else ""
            except (ValueError, TypeError):
                body_msg = " — " + r.stdout.strip()
        raise RuntimeError(
            f"inline post failed for {path}:{line} @ {commit_sha[:7]}: "
            f"{stderr_msg}{body_msg}"
        )
    data = json.loads(r.stdout)
    return data["id"]


def post_issue_comment(number, body):
    """Post a general comment on the PR conversation (not tied to a line)."""
    r = subprocess.run(
        [
            "gh", "api", f"repos/{_repo()}/issues/{number}/comments",
            "-f", f"body={body}",
        ],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        detail = (r.stderr or "").strip() or (r.stdout or "").strip()
        raise RuntimeError(f"comment post failed on #{number}: {detail}")
    return json.loads(r.stdout)["id"]


def reply_to_inline_comment(number, comment_id, body):
    """Post `body` as a threaded reply under an existing inline comment.

    Used for the plain-English follow-up: the technical comment lands first,
    this one hangs underneath it in the same thread. Returns the reply id.
    """
    r = subprocess.run(
        [
            "gh", "api",
            f"repos/{_repo()}/pulls/{number}/comments/{comment_id}/replies",
            "-f", f"body={body}",
        ],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        detail = (r.stderr or "").strip() or (r.stdout or "").strip()
        raise RuntimeError(
            f"plain-English reply failed on comment {comment_id}: {detail}"
        )
    return json.loads(r.stdout)["id"]


def approve_pr(number, body=None):
    cmd = ["gh", "pr", "review", str(number), "--repo", _repo(), "--approve"]
    if body:
        cmd += ["--body", body]
    _run(cmd)
