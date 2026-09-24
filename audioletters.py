import sqlite3
import re

from botocore.exceptions import BotoCoreError, ClientError
from flask import Blueprint, abort, current_app, flash, make_response, redirect, render_template, request, Response, stream_with_context, url_for

from auth import admin_required, current_subscriber_id, paid_subscriber_required
from audio_storage import AudioStorageUnavailable, fetch_audio
from db import connect, transaction, utcnow
from services import can_access_paid_audioletter


audioletters_bp = Blueprint("audioletters", __name__)
_BYTE_RANGE = re.compile(r"bytes=(?:[0-9]+-[0-9]*|-[1-9][0-9]*)\Z", re.ASCII)


def accessible_audioletter_episode(conn, subscriber_id, episode_id):
    """Return content only when the subscriber can access this published episode."""
    subscriber = conn.execute(
        "SELECT is_active,is_paid_subscriber,accessible_through FROM subscribers WHERE id=?",
        (subscriber_id,),
    ).fetchone()
    episode = conn.execute(
        "SELECT * FROM audioletter_episodes WHERE id=?", (episode_id,)
    ).fetchone()
    if episode is None or not can_access_paid_audioletter(
        subscriber, episode["sequence"], episode["is_published"]
    ):
        return None
    return episode


def episode_blocks(conn, episode):
    """Older single-audio episodes remain readable without rewriting production rows."""
    rows = conn.execute(
        "SELECT * FROM audioletter_blocks WHERE episode_id=? ORDER BY sort_order,id",
        (episode["id"],),
    ).fetchall()
    if rows:
        return rows
    if episode["audio_storage_key"] or episode["transcript"]:
        return [{"id": None, "block_type": "audio", "title": "", "body": "",
                 "audio_storage_key": episode["audio_storage_key"],
                 "transcript": episode["transcript"], "sort_order": 1}]
    return []


def _promote_legacy_block(conn, episode):
    """Only materialize legacy content when an operator first adds a block."""
    if not (episode["audio_storage_key"] or episode["transcript"]):
        return
    if conn.execute("SELECT 1 FROM audioletter_blocks WHERE episode_id=?",
                    (episode["id"],)).fetchone():
        return
    now = utcnow()
    conn.execute(
        """INSERT INTO audioletter_blocks
           (episode_id,sort_order,block_type,title,audio_storage_key,transcript,created_at,updated_at)
           VALUES(?,1,'audio','메인 오디오',?,?,?,?)""",
        (episode["id"], episode["audio_storage_key"], episode["transcript"], now, now),
    )


def _private_page(template, **values):
    response = make_response(render_template(template, **values))
    response.headers["Cache-Control"] = "private, no-store"
    return response


@audioletters_bp.get("/audioletters")
@paid_subscriber_required
def audioletter_list():
    conn = connect(current_app.config["DB_PATH"])
    rows = conn.execute(
        """SELECT e.id,e.sequence,e.season,e.season_episode,e.title
           FROM audioletter_episodes e JOIN subscribers s ON s.id=?
           WHERE s.is_active=1 AND s.is_paid_subscriber=1
             AND s.accessible_through IS NOT NULL
             AND e.is_published=1 AND e.sequence<=s.accessible_through
           ORDER BY e.sequence""", (current_subscriber_id(),)
    ).fetchall()
    conn.close()
    return _private_page("audioletter_list.html", episodes=rows)


@audioletters_bp.get("/audioletters/<int:episode_id>")
@paid_subscriber_required
def audioletter_detail(episode_id):
    conn = connect(current_app.config["DB_PATH"])
    episode = accessible_audioletter_episode(conn, current_subscriber_id(), episode_id)
    blocks = episode_blocks(conn, episode) if episode else []
    quiz = None
    participated = False
    if episode and episode["quiz_episode_code"]:
        quiz = conn.execute(
            """SELECT e.code,e.id,COUNT(q.id) question_count
               FROM episodes e LEFT JOIN questions q ON q.episode_id=e.id
               WHERE e.code=? AND e.is_published=1 GROUP BY e.id""",
            (episode["quiz_episode_code"],),
        ).fetchone()
        if quiz and quiz["question_count"]:
            participated = conn.execute(
                "SELECT 1 FROM participation WHERE episode_id=? AND subscriber_id=?",
                (quiz["id"], current_subscriber_id()),
            ).fetchone() is not None
    conn.close()
    if episode is None:
        abort(404)
    return _private_page("audioletter_detail.html", episode=episode, blocks=blocks,
                         quiz=quiz if quiz and quiz["question_count"] else None,
                         participated=participated)


@audioletters_bp.get("/audioletters/<int:episode_id>/audio")
@paid_subscriber_required
def audioletter_audio(episode_id):
    conn = connect(current_app.config["DB_PATH"])
    episode = accessible_audioletter_episode(conn, current_subscriber_id(), episode_id)
    blocks = episode_blocks(conn, episode) if episode else []
    conn.close()
    # Keep the original URL working for the first audio, including legacy episode 7.
    first = next((b for b in blocks if b["block_type"] == "audio"), None)
    if first is None or not first["audio_storage_key"]:
        abort(404)
    return _stream_audio(first["audio_storage_key"], episode_id)


@audioletters_bp.get("/audioletters/<int:episode_id>/blocks/<int:block_id>/audio")
@paid_subscriber_required
def audioletter_block_audio(episode_id, block_id):
    conn = connect(current_app.config["DB_PATH"])
    episode = accessible_audioletter_episode(conn, current_subscriber_id(), episode_id)
    block = conn.execute(
        """SELECT audio_storage_key FROM audioletter_blocks
           WHERE id=? AND episode_id=? AND block_type='audio'""",
        (block_id, episode_id),
    ).fetchone() if episode else None
    conn.close()
    if block is None or not block["audio_storage_key"]:
        abort(404)
    return _stream_audio(block["audio_storage_key"], episode_id)


def _stream_audio(key, episode_id):
    # Treat the field as an object key, never as a URL or a filesystem path.
    if (key.startswith("/") or "://" in key or "\\" in key
            or any(ord(c) < 32 for c in key)):
        current_app.logger.warning("audioletter_invalid_storage_key episode_id=%s", episode_id)
        abort(404)

    requested_range = request.headers.get("Range")
    if requested_range is not None and not _BYTE_RANGE.fullmatch(requested_range):
        return Response(status=416, headers={"Cache-Control": "private, no-store"})
    try:
        obj = fetch_audio(current_app.config, key, requested_range)
    except AudioStorageUnavailable:
        current_app.logger.error("audioletter_bucket_unconfigured episode_id=%s", episode_id)
        abort(503)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in {"NoSuchKey", "404", "NoSuchBucket"}:
            current_app.logger.warning("audioletter_object_missing episode_id=%s", episode_id)
            abort(404)
        if code in {"InvalidRange", "416", "RequestedRangeNotSatisfiable"}:
            return Response(status=416, headers={"Cache-Control": "private, no-store"})
        current_app.logger.error("audioletter_bucket_read_failed episode_id=%s code=%s", episode_id, code)
        abort(503)
    except BotoCoreError as exc:
        current_app.logger.error("audioletter_bucket_read_failed episode_id=%s reason=%s", episode_id, type(exc).__name__)
        abort(503)

    status = obj.get("ResponseMetadata", {}).get("HTTPStatusCode", 200)
    content_range = obj.get("ContentRange")
    if (requested_range and (status != 206 or not content_range or
                             not re.fullmatch(r"bytes [0-9]+-[0-9]+/[0-9]+", content_range))):
        obj["Body"].close()
        current_app.logger.error("audioletter_bucket_invalid_range_response episode_id=%s", episode_id)
        abort(503)
    if status not in {200, 206}:
        obj["Body"].close()
        abort(503)
    body = obj["Body"]

    def chunks():
        try:
            yield from body.iter_chunks(chunk_size=64 * 1024)
        finally:
            body.close()

    response = Response(stream_with_context(chunks()), status=206 if requested_range else 200,
                        mimetype="audio/mpeg", direct_passthrough=True)
    response.headers["Accept-Ranges"] = "bytes"
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Disposition"] = "inline"
    if content_range and requested_range:
        response.headers["Content-Range"] = content_range
    if obj.get("ContentLength") is not None:
        response.content_length = int(obj["ContentLength"])
    return response


def _positive_integer(name, label, errors):
    raw = request.form.get(name, "").strip()
    if not raw.isascii() or not raw.isdecimal() or len(raw) > 19:
        errors.append(f"{label}는 1 이상의 정수여야 합니다.")
        return None
    value = int(raw)
    if not 1 <= value <= 2**63 - 1:
        errors.append(f"{label}는 1 이상의 정수여야 합니다.")
        return None
    return value


def _form_values():
    errors = []
    values = {
        "sequence": _positive_integer("sequence", "내부 회차", errors),
        "season": _positive_integer("season", "시즌", errors),
        "season_episode": _positive_integer("season_episode", "시즌 내 회차", errors),
        "title": request.form.get("title", "").strip(),
        "audio_storage_key": request.form.get("audio_storage_key", "").strip() or None,
        # Do not strip or convert to HTML: preserve the original line breaks.
        "transcript": request.form.get("transcript", ""),
        "quiz_episode_code": request.form.get("quiz_episode_code", "").strip().upper() or None,
        "is_published": int("is_published" in request.form),
    }
    if not values["title"] or len(values["title"]) > 200:
        errors.append("제목은 1~200자로 입력해주세요.")
    if values["quiz_episode_code"]:
        conn = connect(current_app.config["DB_PATH"])
        found = conn.execute("SELECT 1 FROM episodes WHERE code=?",
                             (values["quiz_episode_code"],)).fetchone()
        conn.close()
        if not found:
            errors.append("연결할 기존 Quiz 회차 코드를 확인해주세요.")
    return values, errors


def _block_values():
    errors = []
    block_type = request.form.get("block_type", "")
    values = {
        "sort_order": _positive_integer("sort_order", "표시 순서", errors),
        "block_type": block_type,
        "title": request.form.get("title", "").strip(),
        "body": request.form.get("body", ""),
        "audio_storage_key": request.form.get("audio_storage_key", "").strip() or None,
        "transcript": request.form.get("transcript", ""),
    }
    if block_type not in {"audio", "info"}:
        errors.append("오디오 또는 정보 블록을 선택해주세요.")
    if len(values["title"]) > 200:
        errors.append("블록 제목은 200자 이하로 입력해주세요.")
    if block_type == "info":
        if not values["body"].strip():
            errors.append("정보 블록의 내용을 입력해주세요.")
        values["audio_storage_key"] = None
        values["transcript"] = ""
    elif block_type == "audio":
        if not values["audio_storage_key"]:
            errors.append("오디오 블록의 저장 키를 입력해주세요.")
        elif (values["audio_storage_key"].startswith("/")
              or "://" in values["audio_storage_key"]
              or "\\" in values["audio_storage_key"]
              or any(ord(c) < 32 for c in values["audio_storage_key"])):
            errors.append("공개 URL이 아닌 비공개 Bucket object key를 입력해주세요.")
        values["body"] = ""
    return values, errors


@audioletters_bp.get("/admin/audioletters")
@admin_required
def admin_audioletter_list():
    conn = connect(current_app.config["DB_PATH"])
    rows = conn.execute(
        """SELECT id,sequence,season,season_episode,title,is_published,
                  audio_storage_key,created_at
           FROM audioletter_episodes ORDER BY sequence"""
    ).fetchall()
    conn.close()
    return render_template("admin_audioletters.html", episodes=rows)


def _admin_episode(episode_id):
    conn = connect(current_app.config["DB_PATH"])
    row = conn.execute("SELECT * FROM audioletter_episodes WHERE id=?", (episode_id,)).fetchone()
    conn.close()
    if row is None:
        abort(404)
    return row


@audioletters_bp.route("/admin/audioletters/<int:episode_id>/blocks/new", methods=["GET", "POST"])
@admin_required
def admin_audioletter_block_new(episode_id):
    episode = _admin_episode(episode_id)
    conn = connect(current_app.config["DB_PATH"])
    max_order = conn.execute("SELECT MAX(sort_order) FROM audioletter_blocks WHERE episode_id=?",
                             (episode_id,)).fetchone()[0]
    conn.close()
    block = {"sort_order": (max_order or (1 if episode["audio_storage_key"] or episode["transcript"] else 0)) + 1,
             "block_type": "audio", "title": "", "body": "", "audio_storage_key": None,
             "transcript": ""}
    if request.method == "POST":
        values, errors = _block_values()
        block = values
        if not errors:
            try:
                with transaction(current_app.config["DB_PATH"]) as conn:
                    _promote_legacy_block(conn, episode)
                    now = utcnow()
                    conn.execute(
                        """INSERT INTO audioletter_blocks
                           (episode_id,sort_order,block_type,title,body,audio_storage_key,
                            transcript,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                        (episode_id, values["sort_order"], values["block_type"],
                         values["title"], values["body"], values["audio_storage_key"],
                         values["transcript"], now, now),
                    )
            except sqlite3.IntegrityError:
                errors.append("이미 사용 중인 표시 순서입니다.")
            else:
                return redirect(url_for("audioletters.admin_audioletter_edit", episode_id=episode_id))
        for error in errors:
            flash(error, "error")
    return render_template("admin_audioletter_block_form.html", episode=episode, block=block)


@audioletters_bp.route("/admin/audioletters/<int:episode_id>/blocks/<int:block_id>/edit",
                       methods=["GET", "POST"])
@admin_required
def admin_audioletter_block_edit(episode_id, block_id):
    episode = _admin_episode(episode_id)
    conn = connect(current_app.config["DB_PATH"])
    block = conn.execute("SELECT * FROM audioletter_blocks WHERE id=? AND episode_id=?",
                         (block_id, episode_id)).fetchone()
    conn.close()
    if block is None:
        abort(404)
    if request.method == "POST":
        values, errors = _block_values()
        if not errors:
            try:
                with transaction(current_app.config["DB_PATH"]) as conn:
                    conn.execute(
                        """UPDATE audioletter_blocks
                           SET sort_order=?,block_type=?,title=?,body=?,audio_storage_key=?,
                               transcript=?,updated_at=? WHERE id=? AND episode_id=?""",
                        (values["sort_order"], values["block_type"], values["title"],
                         values["body"], values["audio_storage_key"],values["transcript"],
                         utcnow(), block_id, episode_id),
                    )
            except sqlite3.IntegrityError:
                errors.append("이미 사용 중인 표시 순서입니다.")
            else:
                return redirect(url_for("audioletters.admin_audioletter_edit", episode_id=episode_id))
        for error in errors:
            flash(error, "error")
        block = values
    return render_template("admin_audioletter_block_form.html", episode=episode,
                           block=block, block_id=block_id)


@audioletters_bp.post("/admin/audioletters/<int:episode_id>/blocks/<int:block_id>/delete")
@admin_required
def admin_audioletter_block_delete(episode_id, block_id):
    episode = _admin_episode(episode_id)
    with transaction(current_app.config["DB_PATH"]) as conn:
        block = conn.execute("SELECT id FROM audioletter_blocks WHERE id=? AND episode_id=?",
                             (block_id, episode_id)).fetchone()
        if not block:
            abort(404)
        count = conn.execute("SELECT COUNT(*) FROM audioletter_blocks WHERE episode_id=?",
                             (episode_id,)).fetchone()[0]
        if count == 1 and (episode["audio_storage_key"] or episode["transcript"]):
            flash("기존 오디오 데이터가 있으므로 마지막 블록은 삭제할 수 없습니다.", "error")
        else:
            conn.execute("DELETE FROM audioletter_blocks WHERE id=?", (block_id,))
    return redirect(url_for("audioletters.admin_audioletter_edit", episode_id=episode_id))


@audioletters_bp.route("/admin/audioletters/new", methods=["GET", "POST"])
@admin_required
def admin_audioletter_new():
    episode = None
    if request.method == "POST":
        values, errors = _form_values()
        episode = values
        if not errors:
            try:
                now = utcnow()
                with transaction(current_app.config["DB_PATH"]) as conn:
                    episode_id = conn.execute(
                        """INSERT INTO audioletter_episodes
                           (sequence,season,season_episode,title,audio_storage_key,
                            transcript,quiz_episode_code,is_published,created_at,updated_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        (
                            values["sequence"], values["season"],
                            values["season_episode"], values["title"],
                            values["audio_storage_key"], values["transcript"],
                            values["quiz_episode_code"], values["is_published"], now, now,
                        ),
                    ).lastrowid
            except sqlite3.IntegrityError:
                errors.append("내부 회차 또는 시즌 내 회차가 이미 등록됐습니다.")
            else:
                flash("오디오레터 회차를 등록했습니다.", "success")
                return redirect(url_for("audioletters.admin_audioletter_edit", episode_id=episode_id))
        for error in errors:
            flash(error, "error")
    return render_template("admin_audioletter_form.html", episode=episode)


@audioletters_bp.route("/admin/audioletters/<int:episode_id>/edit", methods=["GET", "POST"])
@admin_required
def admin_audioletter_edit(episode_id):
    conn = connect(current_app.config["DB_PATH"])
    stored_episode = conn.execute(
        "SELECT * FROM audioletter_episodes WHERE id=?", (episode_id,)
    ).fetchone()
    conn.close()
    if stored_episode is None:
        abort(404)

    episode = stored_episode
    conn = connect(current_app.config["DB_PATH"])
    blocks = conn.execute("SELECT * FROM audioletter_blocks WHERE episode_id=? ORDER BY sort_order,id",
                          (episode_id,)).fetchall()
    conn.close()
    if request.method == "POST":
        values, errors = _form_values()
        if blocks:
            # Legacy episode fields remain for compatibility; blocks are canonical once present.
            values["audio_storage_key"] = stored_episode["audio_storage_key"]
            values["transcript"] = stored_episode["transcript"]
        episode = values
        if not errors:
            try:
                with transaction(current_app.config["DB_PATH"]) as conn:
                    conn.execute(
                        """UPDATE audioletter_episodes
                           SET sequence=?,season=?,season_episode=?,title=?,
                               audio_storage_key=?,transcript=?,quiz_episode_code=?,
                               is_published=?,updated_at=?
                           WHERE id=?""",
                        (
                            values["sequence"], values["season"],
                            values["season_episode"], values["title"],
                            values["audio_storage_key"], values["transcript"],
                            values["quiz_episode_code"], values["is_published"], utcnow(), episode_id,
                        ),
                    )
            except sqlite3.IntegrityError:
                errors.append("내부 회차 또는 시즌 내 회차가 이미 등록됐습니다.")
            else:
                flash("오디오레터 회차를 저장했습니다.", "success")
                return redirect(url_for("audioletters.admin_audioletter_edit", episode_id=episode_id))
        for error in errors:
            flash(error, "error")
    return render_template("admin_audioletter_form.html", episode=episode, episode_id=episode_id,
                           blocks=blocks)
