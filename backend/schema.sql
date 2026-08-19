PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS prs (
  number INTEGER PRIMARY KEY,
  author TEXT NOT NULL,
  title TEXT NOT NULL,
  url TEXT NOT NULL,
  head_sha TEXT,
  status TEXT NOT NULL DEFAULT 'queued',
    -- queued: detected, not yet reviewed
    -- awaiting_user: findings ready for user decision
    -- pending_author: comments posted, waiting on author
  jira_key TEXT,
  jira_summary TEXT,
  has_new_activity INTEGER NOT NULL DEFAULT 0,
  last_seen_commit_sha TEXT,
  last_seen_review_comment_at TEXT,
  last_seen_issue_comment_at TEXT,
  pinned_at TEXT,
  parked INTEGER NOT NULL DEFAULT 0,
  chat_session_id TEXT,
  created_at TEXT DEFAULT (datetime('now')),
  updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS findings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pr_number INTEGER NOT NULL REFERENCES prs(number) ON DELETE CASCADE,
  severity TEXT NOT NULL, -- critical | important | suggestion
  file TEXT,
  line INTEGER,
  title TEXT NOT NULL,
  message TEXT NOT NULL,
  code_snippet TEXT,
  blast_radius TEXT,
  confidence TEXT,
  fix TEXT,
  suggestion_body TEXT, -- full markdown body to post inline
  -- Plain-English layer. Same finding, retold for a non-technical reader.
  -- Produced by the same review pass, so no second Claude call.
  plain_verdict TEXT,     -- Real bug | Your call | Worth tidying | Style point
  plain_title TEXT,       -- headline with no jargon
  plain_summary TEXT,     -- what is happening, in one or two sentences
  plain_impact_label TEXT,-- "Why it matters" or "The decision"
  plain_impact TEXT,      -- consequence, or the judgement call to make
  plain_body TEXT,        -- markdown follow-up comment posted under the technical one
  status TEXT NOT NULL DEFAULT 'pending', -- pending | posted | skipped
  github_comment_id INTEGER,
  created_at TEXT DEFAULT (datetime('now'))
);

-- One Claude conversation per PR, opened from the findings list. The session
-- id is the CLI's own session uuid, so turns resume with full context rather
-- than replaying the transcript on every message.
CREATE TABLE IF NOT EXISTS chat_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pr_number INTEGER NOT NULL REFERENCES prs(number) ON DELETE CASCADE,
  role TEXT NOT NULL, -- user | assistant
  content TEXT NOT NULL,
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS watcher_runs (
  name TEXT PRIMARY KEY,
  last_run_at TEXT,
  next_run_at TEXT,
  last_result TEXT,
  interval_seconds INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS activity_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pr_number INTEGER,
  action TEXT NOT NULL,
  details TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_findings_pr ON findings(pr_number);
CREATE INDEX IF NOT EXISTS idx_chat_pr ON chat_messages(pr_number);
CREATE INDEX IF NOT EXISTS idx_prs_status ON prs(status);
