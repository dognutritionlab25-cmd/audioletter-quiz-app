import hashlib
import hmac
import json
from db import transaction, utcnow


def subscriber_counts(conn, subscriber_id, season_id=None):
    current_total = conn.execute(
        "SELECT COUNT(*) FROM participation WHERE subscriber_id=?", (subscriber_id,)
    ).fetchone()[0]
    legacy_total = conn.execute(
        "SELECT COALESCE(SUM(participation_count),0) FROM legacy_participation WHERE subscriber_id=?",
        (subscriber_id,),
    ).fetchone()[0]
    if season_id is None:
        season = 0
    else:
        season = conn.execute(
            """SELECT COUNT(*) FROM participation p
               JOIN episodes e ON e.id=p.episode_id
               WHERE p.subscriber_id=? AND e.season_id=?""",
            (subscriber_id, season_id),
        ).fetchone()[0]
    return {"season": season, "total": current_total + legacy_total}


def participation_breakdown(conn, subscriber_id):
    current_total = conn.execute(
        "SELECT COUNT(*) FROM participation WHERE subscriber_id=?", (subscriber_id,)
    ).fetchone()[0]
    legacy_total = conn.execute(
        "SELECT COALESCE(SUM(participation_count),0) FROM legacy_participation WHERE subscriber_id=?",
        (subscriber_id,),
    ).fetchone()[0]
    return {
        "current": current_total,
        "legacy": legacy_total,
        "total": current_total + legacy_total,
    }


def start_attempt(db_path, subscriber_id, episode_id):
    with transaction(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO quiz_attempts(subscriber_id,episode_id,started_at) VALUES(?,?,?)",
            (subscriber_id, episode_id, utcnow()),
        )
        return cursor.lastrowid


def save_answer(db_path, attempt_id, question_id, choice_id):
    with transaction(db_path) as conn:
        choice = conn.execute(
            "SELECT question_id,is_correct FROM choices WHERE id=?", (choice_id,)
        ).fetchone()
        question = conn.execute(
            "SELECT points FROM questions WHERE id=?", (question_id,)
        ).fetchone()
        if not choice or not question or choice["question_id"] != question_id:
            raise ValueError("Invalid choice")
        correct = int(choice["is_correct"])
        awarded = question["points"] if correct else 0
        conn.execute(
            """INSERT INTO attempt_answers(attempt_id,question_id,choice_id,is_correct,points_awarded)
               VALUES(?,?,?,?,?)
               ON CONFLICT(attempt_id,question_id) DO UPDATE SET
               choice_id=excluded.choice_id,is_correct=excluded.is_correct,
               points_awarded=excluded.points_awarded""",
            (attempt_id, question_id, choice_id, correct, awarded),
        )
        return bool(correct)


def complete_attempt(db_path, attempt_id):
    with transaction(db_path) as conn:
        attempt = conn.execute("SELECT * FROM quiz_attempts WHERE id=?", (attempt_id,)).fetchone()
        if not attempt:
            raise ValueError("Attempt not found")
        questions = conn.execute(
            "SELECT id,points FROM questions WHERE episode_id=?", (attempt["episode_id"],)
        ).fetchall()
        answered = conn.execute(
            "SELECT COUNT(*) FROM attempt_answers WHERE attempt_id=?", (attempt_id,)
        ).fetchone()[0]
        if answered != len(questions):
            raise ValueError("All questions must be answered")
        score = conn.execute(
            "SELECT COALESCE(SUM(points_awarded),0) FROM attempt_answers WHERE attempt_id=?",
            (attempt_id,),
        ).fetchone()[0]
        total = sum(row["points"] for row in questions)
        completed_at = utcnow()
        conn.execute(
            "UPDATE quiz_attempts SET completed_at=?,score=?,total_points=?,status='completed' WHERE id=?",
            (completed_at, score, total, attempt_id),
        )
        conn.execute(
            """INSERT OR IGNORE INTO participation
               (subscriber_id,episode_id,first_completed_at,first_attempt_id,source)
               VALUES(?,?,?,?,?)""",
            (attempt["subscriber_id"], attempt["episode_id"], completed_at, attempt_id, "app"),
        )
        return {"score": score, "total": total, "completed_at": completed_at}


def email_hash(email, secret):
    normalized = email.strip().lower().encode("utf-8")
    return hmac.new(secret.encode("utf-8"), normalized, hashlib.sha256).hexdigest()


def save_feedback(db_path, episode_id, subscriber_id, values, source="app"):
    with transaction(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO feedback_submissions(episode_id,subscriber_id,created_at,source) VALUES(?,?,?,?)",
            (episode_id, subscriber_id, utcnow(), source),
        )
        submission_id = cursor.lastrowid
        for question_id, value in values.items():
            if isinstance(value, list):
                conn.execute(
                    "INSERT INTO feedback_answers(submission_id,feedback_question_id,value_json) VALUES(?,?,?)",
                    (submission_id, question_id, json.dumps(value, ensure_ascii=False)),
                )
            else:
                conn.execute(
                    "INSERT INTO feedback_answers(submission_id,feedback_question_id,value_text) VALUES(?,?,?)",
                    (submission_id, question_id, str(value)),
                )
        return submission_id
