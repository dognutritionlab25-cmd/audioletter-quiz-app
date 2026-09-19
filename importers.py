import csv
import io
import json
import re
from datetime import datetime
from db import transaction, utcnow
from services import email_hash


EPISODE_RE = re.compile(r"(?:\[)?(?:S\d+-)?R?(\d{1,3})(?:\])?", re.IGNORECASE)


def episode_code_from_title(title):
    match = EPISODE_RE.search(title or "")
    if not match:
        raise ValueError("Episode code not found in form title")
    return f"R{int(match.group(1)):03d}"


def import_google_form_payload(db_path, payload, season_code="S1", publish=False):
    title = payload.get("info", {}).get("title") or payload.get("info", {}).get("documentTitle") or ""
    code = episode_code_from_title(title)
    with transaction(db_path) as conn:
        season = conn.execute("SELECT id FROM seasons WHERE code=?", (season_code,)).fetchone()
        if not season:
            season_id = conn.execute(
                "INSERT INTO seasons(code,title,is_active,created_at) VALUES(?,?,1,?)",
                (season_code, season_code, utcnow()),
            ).lastrowid
        else:
            season_id = season["id"]
        existing = conn.execute("SELECT id FROM episodes WHERE code=?", (code,)).fetchone()
        if existing:
            episode_id = existing["id"]
            conn.execute("DELETE FROM questions WHERE episode_id=?", (episode_id,))
            conn.execute(
                "UPDATE episodes SET title=?,is_published=?,updated_at=? WHERE id=?",
                (title, int(publish), utcnow(), episode_id),
            )
        else:
            episode_id = conn.execute(
                """INSERT INTO episodes(season_id,code,title,is_published,created_at,updated_at)
                   VALUES(?,?,?,?,?,?)""",
                (season_id, code, title, int(publish), utcnow(), utcnow()),
            ).lastrowid
        imported = 0
        for order, item in enumerate(payload.get("items", []), start=1):
            question = item.get("questionItem", {}).get("question")
            if not question or "choiceQuestion" not in question:
                continue
            grading = question.get("grading") or {}
            correct_values = {
                answer.get("value") for answer in grading.get("correctAnswers", {}).get("answers", [])
            }
            feedback = grading.get("whenWrong") or grading.get("whenRight") or {}
            explanation = feedback.get("text") or None
            question_id = conn.execute(
                "INSERT INTO questions(episode_id,text,points,explanation,display_order) VALUES(?,?,?,?,?)",
                (episode_id, item.get("title", ""), int(grading.get("pointValue", 1)), explanation, order),
            ).lastrowid
            for choice_order, option in enumerate(question["choiceQuestion"].get("options", []), start=1):
                value = option.get("value", "")
                conn.execute(
                    "INSERT INTO choices(question_id,text,is_correct,display_order) VALUES(?,?,?,?)",
                    (question_id, value, int(value in correct_values), choice_order),
                )
            imported += 1
        return {"episode_id": episode_id, "episode_code": code, "questions": imported}


def _parse_timestamp(value):
    value = value.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y. %m. %d %p %I:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(value, fmt).isoformat()
        except ValueError:
            pass
    return value


def migrate_historical_responses(db_path, csv_text, episode_code, hash_secret, score_policy="last"):
    if score_policy not in {"last", "first", "max"}:
        raise ValueError("score_policy must be last, first, or max")
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    grouped = {}
    for row in rows:
        email = (row.get("badge_email") or row.get("배지 적립용 이메일") or row.get("email") or "").strip()
        if not email:
            continue
        grouped.setdefault(email.lower(), []).append(row)
    with transaction(db_path) as conn:
        episode = conn.execute("SELECT id FROM episodes WHERE code=?", (episode_code,)).fetchone()
        if not episode:
            raise ValueError("Episode not found")
        migrated = 0
        for email, submissions in grouped.items():
            submissions.sort(key=lambda r: _parse_timestamp(r.get("timestamp") or r.get("타임스탬프") or ""))
            selected = submissions[0] if score_policy == "first" else submissions[-1]
            if score_policy == "max":
                selected = max(submissions, key=lambda r: _score_pair(r)[0])
            digest = email_hash(email, hash_secret)
            subscriber = conn.execute("SELECT id FROM subscribers WHERE email_hash=?", (digest,)).fetchone()
            if subscriber:
                subscriber_id = subscriber["id"]
            else:
                subscriber_id = conn.execute(
                    "INSERT INTO subscribers(public_id,display_name,email_hash,is_test,created_at) VALUES(?,?,?,?,?)",
                    (f"historical-{digest[:16]}", "과거 참여자", digest, 0, utcnow()),
                ).lastrowid
            score, total = _score_pair(selected)
            first_at = _parse_timestamp(submissions[0].get("timestamp") or submissions[0].get("타임스탬프") or utcnow())
            attempt_id = conn.execute(
                """INSERT INTO quiz_attempts(subscriber_id,episode_id,started_at,completed_at,score,total_points,status,source)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (subscriber_id, episode["id"], first_at, first_at, score, total, "completed", "historical"),
            ).lastrowid
            before = conn.total_changes
            conn.execute(
                """INSERT OR IGNORE INTO participation
                   (subscriber_id,episode_id,first_completed_at,first_attempt_id,source)
                   VALUES(?,?,?,?,?)""",
                (subscriber_id, episode["id"], first_at, attempt_id, "historical"),
            )
            if conn.total_changes > before:
                migrated += 1
        return {"rows": len(rows), "identified": len(grouped), "participations_created": migrated}


def _score_pair(row):
    raw = str(row.get("score") or row.get("점수") or "0/0").replace(" ", "")
    parts = raw.split("/")
    try:
        return int(float(parts[0])), int(float(parts[1])) if len(parts) > 1 else 0
    except ValueError:
        return 0, 0


def migrate_anonymous_feedback(db_path, csv_text, episode_code):
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    with transaction(db_path) as conn:
        episode = conn.execute("SELECT id FROM episodes WHERE code=?", (episode_code,)).fetchone()
        if not episode:
            raise ValueError("Episode not found")
        questions = conn.execute(
            "SELECT * FROM feedback_questions WHERE episode_id=? ORDER BY display_order", (episode["id"],)
        ).fetchall()
        for row in rows:
            created_at = _parse_timestamp(row.get("timestamp") or row.get("타임스탬프") or utcnow())
            submission_id = conn.execute(
                "INSERT INTO feedback_submissions(episode_id,subscriber_id,created_at,source) VALUES(?,NULL,?,?)",
                (episode["id"], created_at, "historical_anonymous"),
            ).lastrowid
            values = [row.get("highlights", ""), row.get("satisfaction", ""), row.get("next_topic", "")]
            for question, value in zip(questions, values):
                conn.execute(
                    "INSERT INTO feedback_answers(submission_id,feedback_question_id,value_text) VALUES(?,?,?)",
                    (submission_id, question["id"], value),
                )
        return {"feedback_submissions_created": len(rows)}

