import sqlite3

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for

from auth import admin_required
from db import connect, transaction, utcnow
from services import can_access_paid_audioletter


audioletters_bp = Blueprint("audioletters", __name__)


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
