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
    conn.close()
    if episode is None:
        abort(404)
    return _private_page("audioletter_detail.html", episode=episode)


@audioletters_bp.get("/audioletters/<int:episode_id>/audio")
@paid_subscriber_required
def audioletter_audio(episode_id):
    conn = connect(current_app.config["DB_PATH"])
    episode = accessible_audioletter_episode(conn, current_subscriber_id(), episode_id)
    conn.close()
    if episode is None or not episode["audio_storage_key"]:
        abort(404)
    key = episode["audio_storage_key"]
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
        "is_published": int("is_published" in request.form),
    }
    if not values["title"] or len(values["title"]) > 200:
        errors.append("제목은 1~200자로 입력해주세요.")
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
                            transcript,is_published,created_at,updated_at)
                           VALUES(?,?,?,?,?,?,?,?,?)""",
                        (
                            values["sequence"], values["season"],
                            values["season_episode"], values["title"],
                            values["audio_storage_key"], values["transcript"],
                            values["is_published"], now, now,
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
    if request.method == "POST":
        values, errors = _form_values()
        episode = values
        if not errors:
            try:
                with transaction(current_app.config["DB_PATH"]) as conn:
                    conn.execute(
                        """UPDATE audioletter_episodes
                           SET sequence=?,season=?,season_episode=?,title=?,
                               audio_storage_key=?,transcript=?,is_published=?,updated_at=?
                           WHERE id=?""",
                        (
                            values["sequence"], values["season"],
                            values["season_episode"], values["title"],
                            values["audio_storage_key"], values["transcript"],
                            values["is_published"], utcnow(), episode_id,
                        ),
                    )
            except sqlite3.IntegrityError:
                errors.append("내부 회차 또는 시즌 내 회차가 이미 등록됐습니다.")
            else:
                flash("오디오레터 회차를 저장했습니다.", "success")
                return redirect(url_for("audioletters.admin_audioletter_edit", episode_id=episode_id))
        for error in errors:
            flash(error, "error")
    return render_template("admin_audioletter_form.html", episode=episode, episode_id=episode_id)
