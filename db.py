import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS subscribers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id TEXT NOT NULL UNIQUE,
    display_name TEXT,
    email_hash TEXT UNIQUE,
    is_test INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS seasons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    season_id INTEGER NOT NULL REFERENCES seasons(id),
    code TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    display_order INTEGER NOT NULL DEFAULT 0,
    is_published INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    points INTEGER NOT NULL DEFAULT 1 CHECK(points >= 0),
    explanation TEXT,
    display_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS choices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    is_correct INTEGER NOT NULL DEFAULT 0,
    display_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS quiz_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscriber_id INTEGER NOT NULL REFERENCES subscribers(id),
    episode_id INTEGER NOT NULL REFERENCES episodes(id),
    started_at TEXT NOT NULL,
    completed_at TEXT,
    score INTEGER NOT NULL DEFAULT 0,
    total_points INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'in_progress',
    source TEXT NOT NULL DEFAULT 'app'
);

CREATE TABLE IF NOT EXISTS attempt_answers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id INTEGER NOT NULL REFERENCES quiz_attempts(id) ON DELETE CASCADE,
    question_id INTEGER NOT NULL REFERENCES questions(id),
    choice_id INTEGER REFERENCES choices(id),
    is_correct INTEGER NOT NULL DEFAULT 0,
    points_awarded INTEGER NOT NULL DEFAULT 0,
    UNIQUE(attempt_id, question_id)
);

CREATE TABLE IF NOT EXISTS participation (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscriber_id INTEGER NOT NULL REFERENCES subscribers(id),
    episode_id INTEGER NOT NULL REFERENCES episodes(id),
    first_completed_at TEXT NOT NULL,
    first_attempt_id INTEGER REFERENCES quiz_attempts(id),
    source TEXT NOT NULL DEFAULT 'app',
    UNIQUE(subscriber_id, episode_id)
);

CREATE TABLE IF NOT EXISTS legacy_participation (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscriber_id INTEGER NOT NULL REFERENCES subscribers(id),
    season_code TEXT NOT NULL,
    participation_count INTEGER NOT NULL DEFAULT 0 CHECK(participation_count >= 0),
    note TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(subscriber_id, season_code)
);

CREATE TABLE IF NOT EXISTS magic_link_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscriber_id INTEGER NOT NULL REFERENCES subscribers(id),
    token_hash TEXT NOT NULL UNIQUE,
    redirect_path TEXT NOT NULL DEFAULT '/',
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT
);

CREATE TABLE IF NOT EXISTS feedback_questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    prompt TEXT NOT NULL,
    response_type TEXT NOT NULL CHECK(response_type IN ('multi_choice','rating','text')),
    required INTEGER NOT NULL DEFAULT 0,
    display_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS feedback_options (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    feedback_question_id INTEGER NOT NULL REFERENCES feedback_questions(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    display_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS feedback_submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id INTEGER NOT NULL REFERENCES episodes(id),
    subscriber_id INTEGER REFERENCES subscribers(id),
    created_at TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'app'
);

CREATE TABLE IF NOT EXISTS feedback_answers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id INTEGER NOT NULL REFERENCES feedback_submissions(id) ON DELETE CASCADE,
    feedback_question_id INTEGER NOT NULL REFERENCES feedback_questions(id),
    value_text TEXT,
    value_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_questions_episode ON questions(episode_id, display_order);
CREATE INDEX IF NOT EXISTS idx_attempts_subscriber_episode ON quiz_attempts(subscriber_id, episode_id);
CREATE INDEX IF NOT EXISTS idx_participation_subscriber ON participation(subscriber_id);
CREATE INDEX IF NOT EXISTS idx_legacy_participation_subscriber ON legacy_participation(subscriber_id);
CREATE INDEX IF NOT EXISTS idx_magic_link_tokens_expiry ON magic_link_tokens(expires_at, used_at);
CREATE INDEX IF NOT EXISTS idx_feedback_episode ON feedback_submissions(episode_id);
"""


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def database_path(app=None):
    if app is not None:
        return app.config["DB_PATH"]
    return os.environ.get("DB_PATH", "quiz.db")


def connect(path):
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def transaction(path):
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(path):
    with transaction(path) as conn:
        conn.executescript(SCHEMA)
