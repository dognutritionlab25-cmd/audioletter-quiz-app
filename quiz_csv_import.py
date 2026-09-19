import csv
import io
import json
import re

from db import connect, transaction, utcnow


REQUIRED_COLUMNS = (
    "R코드",
    "회차",
    "Form 제목",
    "문항",
    "질문",
    "선택지(JSON)",
    "정답",
    "정답 해설",
    "배점",
    "Form ID",
)
EPISODE_CODE_RE = re.compile(r"R(\d{3})$")


def parse_quiz_csv(csv_text):
    """Validate an exported quiz CSV without writing to the database."""
    reader = csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff")))
    headers = reader.fieldnames or []
    missing = [column for column in REQUIRED_COLUMNS if column not in headers]
    if missing:
        return [], [{"row": 1, "message": f"필수 열 누락: {', '.join(missing)}"}]

    rows = []
    errors = []
    seen_keys = set()
    episode_metadata = {}
    for csv_row_number, source in enumerate(reader, start=2):
        if not any((value or "").strip() for value in source.values()):
            continue
        row_errors = []
        code = (source.get("R코드") or "").strip().upper()
        code_match = EPISODE_CODE_RE.fullmatch(code)
        episode_number = _positive_integer(source.get("회차"))
        question_order = _positive_integer(source.get("문항"))
        points = _nonnegative_integer(source.get("배점"))
        title = (source.get("Form 제목") or "").strip()
        question_text = (source.get("질문") or "").strip()
        answer = (source.get("정답") or "").strip()
        explanation = (source.get("정답 해설") or "").strip() or None

        if not code_match:
            row_errors.append("R코드는 R001 형식이어야 합니다")
        if episode_number is None:
            row_errors.append("회차는 1 이상의 정수여야 합니다")
        elif code_match and int(code_match.group(1)) != episode_number:
            row_errors.append("R코드와 회차가 일치하지 않습니다")
        if not title:
            row_errors.append("Form 제목이 비어 있습니다")
        if question_order is None:
            row_errors.append("문항은 1 이상의 정수여야 합니다")
        if not question_text:
            row_errors.append("질문이 비어 있습니다")
        if points is None:
            row_errors.append("배점은 0 이상의 정수여야 합니다")

        choices = None
        try:
            choices = json.loads((source.get("선택지(JSON)") or "").strip())
        except (json.JSONDecodeError, TypeError):
            row_errors.append("선택지(JSON)가 올바른 JSON 배열이 아닙니다")
        if choices is not None:
            if not isinstance(choices, list) or len(choices) < 2:
                row_errors.append("선택지는 2개 이상의 JSON 배열이어야 합니다")
            elif not all(isinstance(choice, str) and choice.strip() for choice in choices):
                row_errors.append("모든 선택지는 비어 있지 않은 문자열이어야 합니다")
            else:
                choices = [choice.strip() for choice in choices]
                if choices.count(answer) != 1:
                    row_errors.append("정답은 선택지 문자열 하나와 정확히 일치해야 합니다")

        key = (code, question_order)
        if code and question_order is not None:
            if key in seen_keys:
                row_errors.append("같은 R코드와 문항 번호가 CSV에 중복되어 있습니다")
            seen_keys.add(key)

        if code and title and episode_number is not None:
            metadata = (episode_number, title)
            if code in episode_metadata and episode_metadata[code] != metadata:
                row_errors.append("같은 R코드의 회차 또는 Form 제목이 서로 다릅니다")
            episode_metadata.setdefault(code, metadata)

        if row_errors:
            errors.extend({"row": csv_row_number, "message": message} for message in row_errors)
            continue
        rows.append(
            {
                "csv_row": csv_row_number,
                "code": code,
                "episode_number": episode_number,
                "title": title,
                "question_order": question_order,
                "question_text": question_text,
                "choices": choices,
                "answer": answer,
                "explanation": explanation,
                "points": points,
            }
        )
    if not rows and not errors:
        errors.append({"row": 1, "message": "가져올 데이터 행이 없습니다"})
    return rows, errors


def preview_quiz_import(db_path, rows):
    conn = connect(db_path)
    try:
        return _classify_rows(conn, rows)
    finally:
        conn.close()


def import_quiz_rows(db_path, rows):
    """Add only new episodes/questions; exact matches and conflicts are never changed."""
    with transaction(db_path) as conn:
        preview = _classify_rows(conn, rows)
        season = conn.execute("SELECT id FROM seasons WHERE code='S1'").fetchone()
        season_id = season["id"] if season else conn.execute(
            "INSERT INTO seasons(code,title,is_active,created_at) VALUES('S1','시즌 1',1,?)",
            (utcnow(),),
        ).lastrowid

        episode_ids = {}
        created_episode_codes = set()
        for row in preview["new_questions"]:
            code = row["code"]
            if code not in episode_ids:
                episode = conn.execute("SELECT id FROM episodes WHERE code=?", (code,)).fetchone()
                if episode:
                    episode_ids[code] = episode["id"]
                else:
                    episode_ids[code] = conn.execute(
                        """INSERT INTO episodes
                           (season_id,code,title,description,display_order,is_published,created_at,updated_at)
                           VALUES(?,?,?,'',?,0,?,?)""",
                        (season_id, code, row["title"], row["episode_number"], utcnow(), utcnow()),
                    ).lastrowid
                    created_episode_codes.add(code)
                    _create_default_feedback(conn, episode_ids[code])

            question_id = conn.execute(
                """INSERT INTO questions(episode_id,text,points,explanation,display_order)
                   VALUES(?,?,?,?,?)""",
                (
                    episode_ids[code],
                    row["question_text"],
                    row["points"],
                    row["explanation"],
                    row["question_order"],
                ),
            ).lastrowid
            for order, choice in enumerate(row["choices"], start=1):
                conn.execute(
                    "INSERT INTO choices(question_id,text,is_correct,display_order) VALUES(?,?,?,?)",
                    (question_id, choice, int(choice == row["answer"]), order),
                )
        return {
            "episodes_created": len(created_episode_codes),
            "questions_created": len(preview["new_questions"]),
            "questions_skipped": len(preview["existing_questions"]),
            "conflicts_skipped": len(preview["conflicts"]),
        }


def _classify_rows(conn, rows):
    episode_codes = {row["code"] for row in rows}
    existing_episode_codes = {
        row["code"]
        for row in conn.execute(
            f"SELECT code FROM episodes WHERE code IN ({','.join('?' for _ in episode_codes)})",
            tuple(sorted(episode_codes)),
        ).fetchall()
    } if episode_codes else set()

    new_questions = []
    existing_questions = []
    conflicts = []
    for row in rows:
        question = conn.execute(
            """SELECT q.* FROM questions q JOIN episodes e ON e.id=q.episode_id
               WHERE e.code=? AND q.display_order=? ORDER BY q.id LIMIT 1""",
            (row["code"], row["question_order"]),
        ).fetchone()
        if not question:
            new_questions.append(row)
            continue
        choices = conn.execute(
            "SELECT text,is_correct FROM choices WHERE question_id=? ORDER BY display_order,id",
            (question["id"],),
        ).fetchall()
        stored_choices = [choice["text"] for choice in choices]
        stored_answers = [choice["text"] for choice in choices if choice["is_correct"]]
        exact = (
            question["text"] == row["question_text"]
            and question["points"] == row["points"]
            and (question["explanation"] or None) == row["explanation"]
            and stored_choices == row["choices"]
            and stored_answers == [row["answer"]]
        )
        if exact:
            existing_questions.append(row)
        else:
            conflicts.append(
                {
                    "csv_row": row["csv_row"],
                    "code": row["code"],
                    "question_order": row["question_order"],
                    "message": "기존 문항과 같은 표시 순서지만 내용이 다릅니다",
                }
            )

    return {
        "new_episode_count": len(episode_codes - existing_episode_codes),
        "existing_episode_count": len(episode_codes & existing_episode_codes),
        "new_questions": new_questions,
        "existing_questions": existing_questions,
        "conflicts": conflicts,
    }


def _positive_integer(value):
    number = _integer(value)
    return number if number is not None and number > 0 else None


def _nonnegative_integer(value):
    number = _integer(value)
    return number if number is not None and number >= 0 else None


def _integer(value):
    try:
        number = float(str(value).strip())
        return int(number) if number.is_integer() else None
    except (TypeError, ValueError):
        return None


def _create_default_feedback(conn, episode_id):
    definitions = [
        ("이번 오디오레터에서 인상 깊었던 내용", "multi_choice", 1),
        ("이번 오디오레터에 대한 전반적 만족도", "rating", 2),
        ("다음 오디오레터에서 다뤘으면 하는 내용", "text", 3),
    ]
    question_ids = [
        conn.execute(
            "INSERT INTO feedback_questions(episode_id,prompt,response_type,display_order) VALUES(?,?,?,?)",
            (episode_id, prompt, response_type, order),
        ).lastrowid
        for prompt, response_type, order in definitions
    ]
    for order, label in enumerate(["핵심 개념", "실제 사례", "식단 적용", "보호자 관찰 기준"], start=1):
        conn.execute(
            "INSERT INTO feedback_options(feedback_question_id,label,display_order) VALUES(?,?,?)",
            (question_ids[0], label, order),
        )
    for order in range(1, 6):
        conn.execute(
            "INSERT INTO feedback_options(feedback_question_id,label,display_order) VALUES(?,?,?)",
            (question_ids[1], str(order), order),
        )
