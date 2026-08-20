"""SQLite helpers. One DB file at ~/.pr-watcher/state.db."""
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path.home() / ".pr-watcher" / "state.db"
SCHEMA_PATH = Path(__file__).parent / "schema.sql"


_MIGRATIONS = [
    "ALTER TABLE prs ADD COLUMN last_seen_commit_sha TEXT",
    "ALTER TABLE prs ADD COLUMN last_seen_review_comment_at TEXT",
    "ALTER TABLE prs ADD COLUMN last_seen_issue_comment_at TEXT",
    "ALTER TABLE prs ADD COLUMN pinned_at TEXT",
    "ALTER TABLE prs ADD COLUMN parked INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE findings ADD COLUMN plain_verdict TEXT",
    "ALTER TABLE findings ADD COLUMN plain_title TEXT",
    "ALTER TABLE findings ADD COLUMN plain_summary TEXT",
    "ALTER TABLE findings ADD COLUMN plain_impact_label TEXT",
    "ALTER TABLE findings ADD COLUMN plain_impact TEXT",
    "ALTER TABLE findings ADD COLUMN plain_body TEXT",
    "ALTER TABLE prs ADD COLUMN chat_session_id TEXT",
    # Carry conversations started before chats were addressed by scope over to
    # the scoped tables, so nobody loses a thread they were mid-way through.
    # Both are idempotent: `scope` is the primary key on chat_sessions, and the
    # message copy only fires while the scoped table is still completely empty.
    """INSERT OR IGNORE INTO chat_sessions (scope, session_id)
       SELECT 'pr:' || number, chat_session_id FROM prs
       WHERE chat_session_id IS NOT NULL""",
    """INSERT INTO scoped_chat_messages (scope, role, content, created_at)
       SELECT 'pr:' || pr_number, role, content, created_at FROM chat_messages
       WHERE NOT EXISTS (SELECT 1 FROM scoped_chat_messages)""",
]


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as c:
        c.executescript(SCHEMA_PATH.read_text())
        for sql in _MIGRATIONS:
            try:
                c.execute(sql)
            except sqlite3.OperationalError:
                pass  # column already exists


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON;")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def rows_to_dicts(rows):
    return [dict(r) for r in rows]


def log_action(pr_number, action, details=""):
    with conn() as c:
        c.execute(
            "INSERT INTO activity_log (pr_number, action, details) VALUES (?, ?, ?)",
            (pr_number, action, details),
        )
