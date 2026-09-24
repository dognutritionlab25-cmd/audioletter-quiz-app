import csv
import io
import os
import secrets
import sqlite3
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote, urlsplit

from flask import (
    Flask, Response, abort, flash, redirect, render_template, request, session, url_for
)
from itsdangerous import BadData, URLSafeTimedSerializer

from auth import (
    admin_required,
    current_subscriber_id,
    current_subscriber_is_paid,
    establish_subscriber_session,
    subscriber_required,
)
from audioletters import audioletters_bp
from community import community_bp
from db import connect, init_db, transaction, utcnow
from magic_links import (
    MagicLinkDeliveryError,
    consume_magic_link_token,
    create_magic_link_token,
    invalidate_magic_link_token,
    magic_link_on_cooldown,
    send_magic_link_via_brevo,
)
from presenters import feedback_summary, format_korean_datetime
from public_pages import public_pages_bp
from quiz_csv_import import import_quiz_rows, parse_quiz_csv, preview_quiz_import
from resources import resources_bp
from subscriptions import subscriptions_bp
from services import (
    complete_attempt,
    email_hash,
    participation_breakdown,
    save_answer,
    save_feedback,
    start_attempt,
    subscriber_counts,
)
from subscriber_admin import delete_subscriber_data, subscriber_deletion_summary


BASE_DIR = Path(__file__).resolve().parent
SEASON_ONE_EPISODE_CODES = tuple(f"R{number:03d}" for number in range(1, 43))


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get("APP_SECRET") or secrets.token_hex(32),
        ADMIN_PASSWORD=os.environ.get("ADMIN_PASSWORD", ""),
        DB_PATH=os.environ.get("DB_PATH", str(BASE_DIR / "quiz.db")),
        ENABLE_TEST_IDENTITY=os.environ.get("ENABLE_TEST_IDENTITY", "false").lower() == "true",
        SEED_DEMO_DATA=os.environ.get("SEED_DEMO_DATA", "false").lower() == "true",
        MIGRATION_HASH_SECRET=os.environ.get("MIGRATION_HASH_SECRET") or os.environ.get("APP_SECRET", "dev-only"),
        SUBSCRIBER_SYNC_API_KEY=os.environ.get("SUBSCRIBER_SYNC_API_KEY", ""),
        SUBSCRIPTION_REGISTRATION_API_KEY=os.environ.get(
            "SUBSCRIPTION_REGISTRATION_API_KEY", ""
        ),
        SUBSCRIPTION_REGISTRATION_ENCRYPTION_KEY=os.environ.get(
            "SUBSCRIPTION_REGISTRATION_ENCRYPTION_KEY", ""
        ),
        BREVO_API_KEY=os.environ.get("BREVO_API_KEY", ""),
        MAGIC_LINK_SENDER_EMAIL=os.environ.get("MAGIC_LINK_SENDER_EMAIL", ""),
        MAGIC_LINK_SENDER_NAME=os.environ.get("MAGIC_LINK_SENDER_NAME", ""),
        PUBLIC_BASE_URL=os.environ.get("PUBLIC_BASE_URL", ""),
        MAGIC_LINK_TTL_MINUTES=int(os.environ.get("MAGIC_LINK_TTL_MINUTES", "15")),
        MAGIC_LINK_REQUEST_COOLDOWN_SECONDS=int(
            os.environ.get("MAGIC_LINK_REQUEST_COOLDOWN_SECONDS", "60")
        ),
        BREVO_TIMEOUT_SECONDS=int(os.environ.get("BREVO_TIMEOUT_SECONDS", "10")),
        COMMUNITY_ADMIN_NOTIFICATION_EMAIL=os.environ.get(
            "COMMUNITY_ADMIN_NOTIFICATION_EMAIL", ""
        ),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "false").lower() == "true",
        PERMANENT_SESSION_LIFETIME=timedelta(
            days=int(os.environ.get("SUBSCRIBER_SESSION_DAYS", "180"))
        ),
    )
    if test_config:
        app.config.update(test_config)
    init_db(app.config["DB_PATH"])
    if app.config["SEED_DEMO_DATA"] and not app.config["ENABLE_TEST_IDENTITY"]:
        raise RuntimeError("SEED_DEMO_DATA=true requires ENABLE_TEST_IDENTITY=true")
    if app.config["SEED_DEMO_DATA"]:
        seed_demo(app.config["DB_PATH"])

    def db():
        return connect(app.config["DB_PATH"])

    def csrf_token():
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_urlsafe(32)
        return session["csrf_token"]

    app.jinja_env.globals["csrf_token"] = csrf_token
    app.jinja_env.globals["current_subscriber_is_paid"] = current_subscriber_is_paid
    app.jinja_env.filters["korean_datetime"] = format_korean_datetime
    app.register_blueprint(resources_bp)
    app.register_blueprint(audioletters_bp)
    app.register_blueprint(community_bp)
    app.register_blueprint(public_pages_bp)
    app.register_blueprint(subscriptions_bp)

    @app.before_request
    def verify_csrf():
        # The Make sync endpoint authenticates with its own bearer secret and
        # does not use a browser session. Keep the exemption limited to this
        # single endpoint; all form POST routes retain CSRF protection.
        api_csrf_exempt_endpoints = {
            "subscriber_sync",
            "subscriptions.complete_registration_api",
        }
        if request.method == "POST" and request.endpoint not in api_csrf_exempt_endpoints:
            supplied = request.form.get("csrf_token", "")
            expected = session.get("csrf_token", "")
            if not expected or not secrets.compare_digest(supplied, expected):
                abort(400)

    @app.after_request
    def protect_auth_responses(response):
        if request.path.startswith("/auth/"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/api/subscribers/sync")
    def subscriber_sync():
        configured_key = app.config.get("SUBSCRIBER_SYNC_API_KEY", "")
        if not configured_key:
            return {"error": "subscriber sync is not configured"}, 503

        authorization = request.headers.get("Authorization", "")
        scheme, separator, supplied_key = authorization.partition(" ")
        if (
            not separator
            or scheme.lower() != "bearer"
            or not supplied_key
            or not secrets.compare_digest(supplied_key, configured_key)
        ):
            return {"error": "unauthorized"}, 401

        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return {"error": "request body must be a JSON object"}, 400

        raw_email = payload.get("email")
        if not isinstance(raw_email, str):
            return {"error": "email is required"}, 400
        normalized_email = raw_email.strip().lower()
        if not _looks_like_email(normalized_email):
            return {"error": "invalid email"}, 400

        display_name_supplied = "display_name" in payload
        display_name = payload.get("display_name")
        if display_name_supplied and display_name is not None and not isinstance(display_name, str):
            return {"error": "display_name must be a string or null"}, 400
        if isinstance(display_name, str):
            display_name = display_name.strip() or None

        active_supplied = "active" in payload
        active = payload.get("active")
        if active_supplied and type(active) is not bool:
            return {"error": "active must be a boolean"}, 400

        paid_supplied = "is_paid_subscriber" in payload
        paid = payload.get("is_paid_subscriber")
        if paid_supplied and type(paid) is not bool:
            return {"error": "is_paid_subscriber must be a boolean"}, 400

        through_supplied = "accessible_through" in payload
        accessible_through = payload.get("accessible_through")
        if through_supplied and (
            type(accessible_through) is not int
            or not 0 <= accessible_through <= 2**63 - 1
        ):
            return {"error": "accessible_through must be a non-negative integer"}, 400

        digest = email_hash(normalized_email, app.config["MIGRATION_HASH_SECRET"])
        requested_active = int(active) if active_supplied else 1
        requested_paid = int(paid) if paid_supplied else 0
        requested_through = accessible_through if through_supplied else None
        generated_public_id = f"sub_{secrets.token_urlsafe(12)}"
        changed = False

        with transaction(app.config["DB_PATH"]) as conn:
            inserted = conn.execute(
                """INSERT OR IGNORE INTO subscribers
                   (public_id,display_name,email_hash,is_test,is_active,is_paid_subscriber,accessible_through,created_at)
                   VALUES(?,?,?,0,?,?,?,?)""",
                (
                    generated_public_id,
                    display_name,
                    digest,
                    requested_active,
                    requested_paid,
                    requested_through,
                    utcnow(),
                ),
            ).rowcount == 1
            subscriber = conn.execute(
                """SELECT id,public_id,display_name,is_test,is_active,is_paid_subscriber,accessible_through
                   FROM subscribers WHERE email_hash=?""",
                (digest,),
            ).fetchone()
            if subscriber is None:
                # Defensive response for an unexpected uniqueness conflict.
                return {"error": "subscriber could not be synchronized"}, 409

            if not inserted:
                updates = []
                parameters = []
                # Do not overwrite a name curated in the admin UI. The API may
                # only fill a currently blank name.
                if display_name and not subscriber["display_name"]:
                    updates.append("display_name=?")
                    parameters.append(display_name)
                if active_supplied and subscriber["is_active"] != int(active):
                    updates.append("is_active=?")
                    parameters.append(int(active))
                if (
                    paid_supplied
                    and subscriber["is_paid_subscriber"] != int(paid)
                ):
                    updates.append("is_paid_subscriber=?")
                    parameters.append(int(paid))
                if through_supplied and (
                    subscriber["accessible_through"] is None
                    or accessible_through > subscriber["accessible_through"]
                ):
                    updates.append("accessible_through=?")
                    parameters.append(accessible_through)
                if updates:
                    parameters.append(subscriber["id"])
                    conn.execute(
                        f"UPDATE subscribers SET {','.join(updates)} WHERE id=?",
                        parameters,
                    )
                    changed = True
                if active_supplied and not active:
                    conn.execute(
                        """UPDATE magic_link_tokens SET used_at=?
                           WHERE subscriber_id=? AND used_at IS NULL""",
                        (utcnow(), subscriber["id"]),
                    )
                subscriber = conn.execute(
                    """SELECT id,public_id,display_name,is_test,is_active,is_paid_subscriber,accessible_through
                       FROM subscribers WHERE id=?""",
                    (subscriber["id"],),
                ).fetchone()

            # A registration request is only an application. Linking it to the
            # verified subscriber keeps its dog profile, but paid access is
            # still controlled exclusively by the explicit paid sync field.
            conn.execute(
                """UPDATE subscription_registrations SET subscriber_id=?,updated_at=?
                   WHERE email_hash=? AND subscriber_id IS NULL""",
                (subscriber["id"], utcnow(), digest),
            )
            conn.execute(
                """UPDATE dog_profiles SET subscriber_id=?,updated_at=?
                   WHERE subscriber_id IS NULL AND registration_id IN (
                       SELECT id FROM subscription_registrations WHERE email_hash=?
                   )""",
                (subscriber["id"], utcnow(), digest),
            )

        status = "created" if inserted else "updated" if changed else "unchanged"
        return {
            "status": status,
            "subscriber": {
                "public_id": subscriber["public_id"],
                "active": bool(subscriber["is_active"]),
                "is_paid_subscriber": bool(subscriber["is_paid_subscriber"]),
                "accessible_through": subscriber["accessible_through"],
            },
        }, 201 if inserted else 200

    @app.route("/auth/email", methods=["GET", "POST"])
    def magic_link_request():
        next_path = _safe_next(
            request.form.get("next") if request.method == "POST" else request.args.get("next")
        ) or url_for("index")
        if current_subscriber_id() is not None:
            return redirect(next_path)
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            subscriber = None
            cooldown = False
            if _looks_like_email(email):
                conn = db()
                subscriber = conn.execute(
                    """SELECT id FROM subscribers
                       WHERE email_hash=? AND is_test=0 AND is_active=1""",
                    (email_hash(email, app.config["MIGRATION_HASH_SECRET"]),),
                ).fetchone()
                if subscriber:
                    cooldown = magic_link_on_cooldown(
                        conn,
                        subscriber["id"],
                        app.config["MAGIC_LINK_REQUEST_COOLDOWN_SECONDS"],
                    )
                conn.close()
            if subscriber and not cooldown:
                raw_token = create_magic_link_token(
                    app.config["DB_PATH"],
                    subscriber["id"],
                    next_path,
                    app.config["MAGIC_LINK_TTL_MINUTES"],
                )
                try:
                    magic_url = _public_magic_link_url(app, raw_token)
                    sender = app.config.get("MAGIC_LINK_SENDER") or send_magic_link_via_brevo
                    sender(email, magic_url, app.config)
                except MagicLinkDeliveryError:
                    invalidate_magic_link_token(app.config["DB_PATH"], raw_token)
                    app.logger.error(
                        "magic_link_request_failed reason=delivery subscriber_public_data=omitted"
                    )
                    return render_template("magic_link_error.html", next=next_path), 503
            return render_template("magic_link_sent.html")
        return render_template("magic_link_request.html", next=next_path)

    @app.get("/auth/verify")
    def magic_link_verify():
        result = consume_magic_link_token(
            app.config["DB_PATH"], request.args.get("token", "")
        )
        if not result:
            return render_template("magic_link_invalid.html"), 400
        conn = db()
        subscriber = conn.execute(
            "SELECT id FROM subscribers WHERE id=? AND is_test=0 AND is_active=1",
            (result["subscriber_id"],),
        ).fetchone()
        conn.close()
        if not subscriber:
            return render_template("magic_link_invalid.html"), 400
        establish_subscriber_session(subscriber["id"])
        return redirect(_safe_next(result["redirect_path"]) or url_for("index"))

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.route("/test-identity", methods=["GET", "POST"])
    def test_identity():
        if not app.config["ENABLE_TEST_IDENTITY"]:
            abort(404)
        conn = db()
        people = conn.execute(
            "SELECT * FROM subscribers WHERE is_test=1 AND is_active=1 ORDER BY display_name"
        ).fetchall()
        if request.method == "POST":
            subscriber_id = request.form.get("subscriber_id", type=int)
            valid = conn.execute(
                "SELECT id FROM subscribers WHERE id=? AND is_test=1 AND is_active=1",
                (subscriber_id,),
            ).fetchone()
            conn.close()
            if not valid:
                abort(400)
            establish_subscriber_session(subscriber_id)
            return redirect(_safe_next(request.form.get("next")) or url_for("index"))
        conn.close()
        return render_template("test_identity.html", people=people, next=request.args.get("next", ""))

    @app.post("/logout")
    def logout():
        session.pop("subscriber_id", None)
        return redirect(url_for("index"))

    @app.get("/quiz")
    @subscriber_required
    def quiz_entry():
        code = request.args.get("episode", "").strip().upper()
        conn = db()
        episode = conn.execute(
            """SELECT e.*,s.title season_title FROM episodes e JOIN seasons s ON s.id=e.season_id
               WHERE e.code=? AND e.is_published=1""", (code,)
        ).fetchone()
        if not episode:
            conn.close()
            abort(404)
        question_count = conn.execute(
            "SELECT COUNT(*) FROM questions WHERE episode_id=?", (episode["id"],)
        ).fetchone()[0]
        counts = subscriber_counts(conn, current_subscriber_id(), episode["season_id"])
        participated = conn.execute(
            "SELECT 1 FROM participation WHERE subscriber_id=? AND episode_id=?",
            (current_subscriber_id(), episode["id"]),
        ).fetchone() is not None
        conn.close()
        return render_template(
            "quiz_entry.html", episode=episode, question_count=question_count,
            counts=counts, participated=participated,
        )

    @app.post("/quiz/<code>/start")
    @subscriber_required
    def quiz_start(code):
        conn = db()
        episode = conn.execute(
            "SELECT * FROM episodes WHERE code=? AND is_published=1", (code.upper(),)
        ).fetchone()
        question_count = conn.execute(
            "SELECT COUNT(*) FROM questions WHERE episode_id=?", (episode["id"],)
        ).fetchone()[0] if episode else 0
        conn.close()
        if not episode or question_count == 0:
            abort(404)
        attempt_id = start_attempt(app.config["DB_PATH"], current_subscriber_id(), episode["id"])
        return redirect(url_for("quiz_question", attempt_id=attempt_id, number=1))

    @app.route("/attempt/<int:attempt_id>/question/<int:number>", methods=["GET", "POST"])
    @subscriber_required
    def quiz_question(attempt_id, number):
        conn = db()
        attempt = conn.execute(
            """SELECT a.*,e.code,e.title FROM quiz_attempts a JOIN episodes e ON e.id=a.episode_id
               WHERE a.id=? AND a.subscriber_id=? AND a.status='in_progress'""",
            (attempt_id, current_subscriber_id()),
        ).fetchone()
        if not attempt:
            conn.close()
            abort(404)
        questions = conn.execute(
            "SELECT * FROM questions WHERE episode_id=? ORDER BY display_order,id", (attempt["episode_id"],)
        ).fetchall()
        if number < 1 or number > len(questions):
            conn.close()
            abort(404)
        question = questions[number - 1]
        choices = conn.execute(
            "SELECT * FROM choices WHERE question_id=? ORDER BY display_order,id", (question["id"],)
        ).fetchall()
        if request.method == "POST":
            choice_id = request.form.get("choice_id", type=int)
            if choice_id is None:
                flash("답을 선택해주세요.", "error")
            else:
                try:
                    save_answer(app.config["DB_PATH"], attempt_id, question["id"], choice_id)
                except ValueError:
                    conn.close()
                    abort(400)
                conn.close()
                if number < len(questions):
                    return redirect(url_for("quiz_question", attempt_id=attempt_id, number=number + 1))
                complete_attempt(app.config["DB_PATH"], attempt_id)
                return redirect(url_for("quiz_complete", attempt_id=attempt_id))
        conn.close()
        return render_template(
            "quiz_question.html", attempt=attempt, question=question, choices=choices,
            number=number, total=len(questions),
        )

    @app.get("/attempt/<int:attempt_id>/complete")
    @subscriber_required
    def quiz_complete(attempt_id):
        conn = db()
        attempt = conn.execute(
            """SELECT a.*,e.code,e.title,e.season_id FROM quiz_attempts a JOIN episodes e ON e.id=a.episode_id
               WHERE a.id=? AND a.subscriber_id=? AND a.status='completed'""",
            (attempt_id, current_subscriber_id()),
        ).fetchone()
        if not attempt:
            conn.close()
            abort(404)
        review = conn.execute(
            """SELECT q.text,q.explanation,c.text selected_text,aa.is_correct,correct.text correct_text
               FROM attempt_answers aa JOIN questions q ON q.id=aa.question_id
               LEFT JOIN choices c ON c.id=aa.choice_id
               LEFT JOIN choices correct ON correct.question_id=q.id AND correct.is_correct=1
               WHERE aa.attempt_id=? ORDER BY q.display_order,q.id""",
            (attempt_id,),
        ).fetchall()
        counts = subscriber_counts(conn, current_subscriber_id(), attempt["season_id"])
        conn.close()
        return render_template("quiz_complete.html", attempt=attempt, review=review, counts=counts)

    @app.get("/me")
    @subscriber_required
    def my_history():
        conn = db()
        person = conn.execute("SELECT * FROM subscribers WHERE id=?", (current_subscriber_id(),)).fetchone()
        rows = conn.execute(
            """SELECT e.code,e.title,p.first_completed_at,
               (SELECT a.score || '/' || a.total_points FROM quiz_attempts a
                WHERE a.subscriber_id=p.subscriber_id AND a.episode_id=p.episode_id
                AND a.status='completed' ORDER BY a.completed_at DESC LIMIT 1) latest_score
               FROM participation p JOIN episodes e ON e.id=p.episode_id
               WHERE p.subscriber_id=? ORDER BY p.first_completed_at DESC""",
            (current_subscriber_id(),),
        ).fetchall()
        breakdown = participation_breakdown(conn, current_subscriber_id())
        conn.close()
        return render_template(
            "my_history.html", person=person, rows=rows,
            total=breakdown["total"], breakdown=breakdown,
        )

    @app.route("/episode/<code>/feedback", methods=["GET", "POST"])
    @subscriber_required
    def feedback(code):
        conn = db()
        episode = conn.execute("SELECT * FROM episodes WHERE code=?", (code.upper(),)).fetchone()
        if not episode:
            conn.close()
            abort(404)
        participated = conn.execute(
            "SELECT 1 FROM participation WHERE subscriber_id=? AND episode_id=?",
            (current_subscriber_id(), episode["id"]),
        ).fetchone()
        if not participated:
            conn.close()
            abort(403)
        questions = conn.execute(
            "SELECT * FROM feedback_questions WHERE episode_id=? ORDER BY display_order,id",
            (episode["id"],),
        ).fetchall()
        options = {q["id"]: conn.execute(
            "SELECT * FROM feedback_options WHERE feedback_question_id=? ORDER BY display_order,id", (q["id"],)
        ).fetchall() for q in questions}
        if request.method == "POST":
            values = {}
            for question in questions:
                key = f"feedback_{question['id']}"
                values[question["id"]] = request.form.getlist(key) if question["response_type"] == "multi_choice" else request.form.get(key, "")
            conn.close()
            save_feedback(app.config["DB_PATH"], episode["id"], current_subscriber_id(), values)
            flash("피드백을 저장했습니다. 고맙습니다.", "success")
            return redirect(url_for("my_history"))
        conn.close()
        return render_template("feedback.html", episode=episode, questions=questions, options=options)

    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        if request.method == "POST":
            configured = app.config["ADMIN_PASSWORD"]
            if not configured:
                flash("ADMIN_PASSWORD가 설정되지 않았습니다.", "error")
            elif secrets.compare_digest(request.form.get("password", ""), configured):
                session["is_admin"] = True
                return redirect(_safe_next(request.form.get("next")) or url_for("admin_dashboard"))
            else:
                flash("비밀번호가 맞지 않습니다.", "error")
        return render_template("admin_login.html", next=request.args.get("next", ""))

    @app.post("/admin/logout")
    def admin_logout():
        session.pop("is_admin", None)
        return redirect(url_for("admin_login"))

    @app.get("/admin")
    @admin_required
    def admin_dashboard():
        conn = db()
        subscriber_totals = conn.execute(
            """SELECT COUNT(*) total,
                      COALESCE(SUM(CASE WHEN is_paid_subscriber=1 THEN 1 ELSE 0 END),0) paid,
                      COALESCE(SUM(CASE WHEN is_paid_subscriber=0 THEN 1 ELSE 0 END),0) not_paid,
                      COALESCE(SUM(CASE WHEN is_active=0 THEN 1 ELSE 0 END),0) inactive
               FROM subscribers"""
        ).fetchone()
        totals = {
            "episodes": conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0],
            "participation": conn.execute("SELECT COUNT(*) FROM participation").fetchone()[0],
            "feedback": conn.execute("SELECT COUNT(*) FROM feedback_submissions").fetchone()[0],
        }
        episodes = conn.execute(
            """SELECT e.*,s.title season_title,
               (SELECT COUNT(*) FROM questions q WHERE q.episode_id=e.id) question_count,
               (SELECT COUNT(*) FROM participation p WHERE p.episode_id=e.id) participant_count,
               (SELECT ROUND(AVG(a.score),2) FROM quiz_attempts a WHERE a.episode_id=e.id AND a.status='completed') avg_score
               FROM episodes e JOIN seasons s ON s.id=e.season_id ORDER BY e.display_order,e.code"""
        ).fetchall()
        conn.close()
        return render_template(
            "admin_dashboard.html",
            totals=totals,
            subscriber_totals=subscriber_totals,
            episodes=episodes,
        )

    @app.post("/admin/episodes/publish-season-1")
    @admin_required
    def admin_publish_season_one():
        placeholders = ",".join("?" for _ in SEASON_ONE_EPISODE_CODES)
        with transaction(app.config["DB_PATH"]) as conn:
            existing_codes = {
                row["code"]
                for row in conn.execute(
                    f"SELECT code FROM episodes WHERE code IN ({placeholders})",
                    SEASON_ONE_EPISODE_CODES,
                )
            }
            missing_codes = [
                code for code in SEASON_ONE_EPISODE_CODES if code not in existing_codes
            ]
            if missing_codes:
                flash(
                    "공개 상태를 변경하지 않았습니다. 누락 회차: "
                    + ", ".join(missing_codes),
                    "error",
                )
                return redirect(url_for("admin_dashboard"))

            changed = conn.execute(
                f"""UPDATE episodes SET is_published=1
                    WHERE code IN ({placeholders}) AND is_published<>1""",
                SEASON_ONE_EPISODE_CODES,
            ).rowcount

        flash(
            f"Season 1 R001~R042 공개 완료: {changed}개 회차의 상태를 변경했습니다.",
            "success",
        )
        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/seasons", methods=["GET", "POST"])
    @admin_required
    def admin_seasons():
        if request.method == "POST":
            with transaction(app.config["DB_PATH"]) as conn:
                conn.execute(
                    "INSERT INTO seasons(code,title,is_active,created_at) VALUES(?,?,?,?)",
                    (request.form["code"].strip().upper(), request.form["title"].strip(), int("is_active" in request.form), utcnow()),
                )
            return redirect(url_for("admin_seasons"))
        conn = db()
        seasons = conn.execute("SELECT * FROM seasons ORDER BY id DESC").fetchall()
        conn.close()
        return render_template("admin_seasons.html", seasons=seasons)

    @app.route("/admin/episodes/new", methods=["GET", "POST"])
    @admin_required
    def admin_episode_new():
        conn = db()
        seasons = conn.execute("SELECT * FROM seasons ORDER BY id DESC").fetchall()
        conn.close()
        if request.method == "POST":
            with transaction(app.config["DB_PATH"]) as conn:
                episode_id = conn.execute(
                    """INSERT INTO episodes(season_id,code,title,description,display_order,is_published,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (request.form.get("season_id", type=int), request.form["code"].strip().upper(),
                     request.form["title"].strip(), request.form.get("description", "").strip(),
                     request.form.get("display_order", type=int, default=0), int("is_published" in request.form), utcnow(), utcnow()),
                ).lastrowid
                create_default_feedback(conn, episode_id)
            return redirect(url_for("admin_episode_edit", episode_id=episode_id))
        return render_template("admin_episode_form.html", seasons=seasons, episode=None)

    @app.route("/admin/quiz-import", methods=["GET", "POST"])
    @admin_required
    def admin_quiz_import():
        if request.method == "GET":
            return render_template("admin_quiz_import.html", preview=None)

        action = request.form.get("action")
        serializer = URLSafeTimedSerializer(app.config["SECRET_KEY"], salt="quiz-csv-import")
        if action == "preview":
            upload = request.files.get("csv_file")
            if not upload or not upload.filename:
                flash("CSV 파일을 선택해주세요.", "error")
                return redirect(url_for("admin_quiz_import"))
            try:
                csv_text = upload.read().decode("utf-8-sig")
            except UnicodeDecodeError:
                flash("CSV 파일은 UTF-8 형식이어야 합니다.", "error")
                return redirect(url_for("admin_quiz_import"))
            rows, errors = parse_quiz_csv(csv_text)
            preview = preview_quiz_import(app.config["DB_PATH"], rows)
            payload = serializer.dumps(rows) if not errors else None
            return render_template(
                "admin_quiz_import.html",
                preview=preview,
                errors=errors,
                payload=payload,
            )

        if action == "import":
            try:
                rows = serializer.loads(request.form.get("payload", ""), max_age=1800)
            except BadData:
                flash("Preview가 만료되었거나 유효하지 않습니다. CSV를 다시 확인해주세요.", "error")
                return redirect(url_for("admin_quiz_import"))
            result = import_quiz_rows(app.config["DB_PATH"], rows)
            flash(
                f"Import 완료: Episode {result['episodes_created']}개, Question "
                f"{result['questions_created']}개 생성 · 기존 동일 {result['questions_skipped']}개, "
                f"충돌 {result['conflicts_skipped']}개 건너뜀",
                "success",
            )
            return redirect(url_for("admin_dashboard"))
        abort(400)

    @app.route("/admin/episodes/<int:episode_id>", methods=["GET", "POST"])
    @admin_required
    def admin_episode_edit(episode_id):
        if request.method == "POST":
            with transaction(app.config["DB_PATH"]) as conn:
                conn.execute(
                    """UPDATE episodes SET season_id=?,code=?,title=?,description=?,display_order=?,is_published=?,updated_at=? WHERE id=?""",
                    (request.form.get("season_id", type=int), request.form["code"].strip().upper(),
                     request.form["title"].strip(), request.form.get("description", "").strip(),
                     request.form.get("display_order", type=int, default=0), int("is_published" in request.form), utcnow(), episode_id),
                )
            flash("회차를 저장했습니다.", "success")
            return redirect(url_for("admin_episode_edit", episode_id=episode_id))
        conn = db()
        episode = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
        seasons = conn.execute("SELECT * FROM seasons ORDER BY id DESC").fetchall()
        questions = conn.execute("SELECT * FROM questions WHERE episode_id=? ORDER BY display_order,id", (episode_id,)).fetchall()
        conn.close()
        if not episode:
            abort(404)
        return render_template("admin_episode_form.html", seasons=seasons, episode=episode, questions=questions)

    @app.route("/admin/episodes/<int:episode_id>/questions/new", methods=["GET", "POST"])
    @app.route("/admin/questions/<int:question_id>/edit", methods=["GET", "POST"])
    @admin_required
    def admin_question_form(episode_id=None, question_id=None):
        conn = db()
        question = conn.execute("SELECT * FROM questions WHERE id=?", (question_id,)).fetchone() if question_id else None
        if question:
            episode_id = question["episode_id"]
        choices = conn.execute("SELECT * FROM choices WHERE question_id=? ORDER BY display_order,id", (question_id,)).fetchall() if question else []
        conn.close()
        if request.method == "POST":
            choice_lines = [line.strip() for line in request.form.get("choices", "").splitlines() if line.strip()]
            correct_index = request.form.get("correct_index", type=int)
            if len(choice_lines) < 2 or correct_index is None or correct_index < 1 or correct_index > len(choice_lines):
                flash("선택지는 2개 이상이며 정답 번호가 선택지 범위 안에 있어야 합니다.", "error")
            else:
                with transaction(app.config["DB_PATH"]) as conn:
                    if question:
                        conn.execute(
                            "UPDATE questions SET text=?,points=?,explanation=?,display_order=? WHERE id=?",
                            (request.form["text"].strip(), request.form.get("points", type=int, default=1),
                             request.form.get("explanation", "").strip() or None,
                             request.form.get("display_order", type=int, default=0), question_id),
                        )
                        conn.execute("DELETE FROM choices WHERE question_id=?", (question_id,))
                    else:
                        question_id = conn.execute(
                            "INSERT INTO questions(episode_id,text,points,explanation,display_order) VALUES(?,?,?,?,?)",
                            (episode_id, request.form["text"].strip(), request.form.get("points", type=int, default=1),
                             request.form.get("explanation", "").strip() or None,
                             request.form.get("display_order", type=int, default=0)),
                        ).lastrowid
                    for index, label in enumerate(choice_lines, start=1):
                        conn.execute(
                            "INSERT INTO choices(question_id,text,is_correct,display_order) VALUES(?,?,?,?)",
                            (question_id, label, int(index == correct_index), index),
                        )
                return redirect(url_for("admin_episode_edit", episode_id=episode_id))
        return render_template(
            "admin_question_form.html", episode_id=episode_id, question=question, choices=choices,
        )

    @app.post("/admin/questions/<int:question_id>/delete")
    @admin_required
    def admin_question_delete(question_id):
        with transaction(app.config["DB_PATH"]) as conn:
            question = conn.execute("SELECT episode_id FROM questions WHERE id=?", (question_id,)).fetchone()
            if not question:
                abort(404)
            # Completed attempts reference both the question and its selected choice
            # without ON DELETE CASCADE. Remove only those question-level answer rows;
            # keep the attempt summary, participation, feedback, and subscriber data.
            conn.execute("DELETE FROM attempt_answers WHERE question_id=?", (question_id,))
            conn.execute("DELETE FROM questions WHERE id=?", (question_id,))
        return redirect(url_for("admin_episode_edit", episode_id=question["episode_id"]))

    @app.get("/admin/subscribers")
    @admin_required
    def admin_subscribers():
        conn = db()
        totals = conn.execute(
            """SELECT COUNT(*) total,
                      COALESCE(SUM(CASE WHEN is_paid_subscriber=1 THEN 1 ELSE 0 END),0) paid,
                      COALESCE(SUM(CASE WHEN is_paid_subscriber=0 THEN 1 ELSE 0 END),0) not_paid,
                      COALESCE(SUM(CASE WHEN is_active=0 THEN 1 ELSE 0 END),0) inactive
               FROM subscribers"""
        ).fetchone()
        rows = conn.execute(
            """SELECT s.id,s.public_id,s.display_name,s.is_test,s.is_active,
               s.is_paid_subscriber,
               (SELECT COUNT(*) FROM participation p WHERE p.subscriber_id=s.id) participation_count,
               (SELECT COALESCE(SUM(lp.participation_count),0) FROM legacy_participation lp
                WHERE lp.subscriber_id=s.id) legacy_count,
               (SELECT MAX(p.first_completed_at) FROM participation p
                WHERE p.subscriber_id=s.id) last_participation
               FROM subscribers s ORDER BY
               (participation_count + legacy_count) DESC,s.id"""
        ).fetchall()
        conn.close()
        return render_template("admin_subscribers.html", rows=rows, totals=totals)

    @app.route("/admin/subscribers/new", methods=["GET", "POST"])
    @admin_required
    def admin_subscriber_new():
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            display_name = request.form.get("display_name", "").strip() or None
            is_active = int("is_active" in request.form)
            if not _looks_like_email(email):
                flash("올바른 이메일 주소를 입력해주세요.", "error")
            else:
                try:
                    with transaction(app.config["DB_PATH"]) as conn:
                        conn.execute(
                            """INSERT INTO subscribers
                               (public_id,display_name,email_hash,is_test,is_active,created_at)
                               VALUES(?,?,?,0,?,?)""",
                            (
                                f"sub_{secrets.token_urlsafe(12)}",
                                display_name,
                                email_hash(email, app.config["MIGRATION_HASH_SECRET"]),
                                is_active,
                                utcnow(),
                            ),
                        )
                    flash("구독자를 등록했습니다.", "success")
                    return redirect(url_for("admin_subscribers"))
                except sqlite3.IntegrityError:
                    flash("이미 등록된 이메일입니다.", "error")
        return render_template("admin_subscriber_new.html")

    @app.route("/admin/subscribers/<int:subscriber_id>", methods=["GET", "POST"])
    @admin_required
    def admin_subscriber_detail(subscriber_id):
        conn = db()
        person = conn.execute(
            "SELECT * FROM subscribers WHERE id=?", (subscriber_id,)
        ).fetchone()
        conn.close()
        if not person:
            abort(404)
        if request.method == "POST":
            action = request.form.get("action", "legacy")
            if action == "status":
                is_active = int("is_active" in request.form)
                with transaction(app.config["DB_PATH"]) as conn:
                    conn.execute(
                        "UPDATE subscribers SET is_active=? WHERE id=?",
                        (is_active, subscriber_id),
                    )
                    if not is_active:
                        conn.execute(
                            """UPDATE magic_link_tokens SET used_at=?
                               WHERE subscriber_id=? AND used_at IS NULL""",
                            (utcnow(), subscriber_id),
                        )
                flash("구독자 상태를 저장했습니다.", "success")
                return redirect(url_for("admin_subscriber_detail", subscriber_id=subscriber_id))
            if action == "legacy":
                count = request.form.get("participation_count", type=int)
                note = request.form.get("note", "").strip() or None
                if count is None or count < 0:
                    flash("과거 참여 횟수는 0 이상의 숫자여야 합니다.", "error")
                else:
                    with transaction(app.config["DB_PATH"]) as conn:
                        conn.execute(
                            """INSERT INTO legacy_participation
                               (subscriber_id,season_code,participation_count,note,updated_at)
                               VALUES(?,?,?,?,?)
                               ON CONFLICT(subscriber_id,season_code) DO UPDATE SET
                               participation_count=excluded.participation_count,
                               note=excluded.note,updated_at=excluded.updated_at""",
                            (subscriber_id, "S1", count, note, utcnow()),
                        )
                    flash("시즌1 과거 참여 기록을 저장했습니다.", "success")
                    return redirect(url_for("admin_subscriber_detail", subscriber_id=subscriber_id))
            else:
                abort(400)
        conn = db()
        legacy = conn.execute(
            "SELECT * FROM legacy_participation WHERE subscriber_id=? AND season_code='S1'",
            (subscriber_id,),
        ).fetchone()
        breakdown = participation_breakdown(conn, subscriber_id)
        activity = conn.execute(
            """SELECT
                   (SELECT COUNT(*) FROM quiz_attempts
                    WHERE subscriber_id=?) quiz_attempts,
                   (SELECT COUNT(*) FROM quiz_attempts
                    WHERE subscriber_id=? AND status='completed') completed_attempts,
                   (SELECT COUNT(*) FROM feedback_submissions
                    WHERE subscriber_id=?) feedback_submissions,
                   (SELECT COUNT(*) FROM community_posts
                    WHERE subscriber_id=?) community_posts,
                   (SELECT COUNT(*) FROM community_comments
                    WHERE subscriber_id=?) community_comments,
                   (SELECT COUNT(*) FROM community_likes
                    WHERE subscriber_id=?) community_likes,
                   (SELECT MAX(first_completed_at) FROM participation
                    WHERE subscriber_id=?) last_participation""",
            (subscriber_id,) * 7,
        ).fetchone()
        participations = conn.execute(
            """SELECT e.code,e.title,p.first_completed_at,p.source
               FROM participation p
               JOIN episodes e ON e.id=p.episode_id
               WHERE p.subscriber_id=?
               ORDER BY p.first_completed_at DESC,p.id DESC""",
            (subscriber_id,),
        ).fetchall()
        conn.close()
        return render_template(
            "admin_subscriber_detail.html",
            person=person,
            legacy=legacy,
            breakdown=breakdown,
            activity=activity,
            participations=participations,
        )

    @app.route(
        "/admin/subscribers/<int:subscriber_id>/delete", methods=["GET", "POST"]
    )
    @admin_required
    def admin_subscriber_delete(subscriber_id):
        conn = db()
        person = conn.execute(
            """SELECT id,public_id,display_name,is_test,is_active,
                      is_paid_subscriber
               FROM subscribers WHERE id=?""",
            (subscriber_id,),
        ).fetchone()
        if not person:
            conn.close()
            abort(404)
        summary = subscriber_deletion_summary(conn, subscriber_id)
        conn.close()

        if request.method == "POST":
            if request.form.get("confirm_delete") != "yes":
                flash("복구 불가 삭제 확인에 동의해야 합니다.", "error")
                return redirect(
                    url_for("admin_subscriber_delete", subscriber_id=subscriber_id)
                )
            try:
                with transaction(app.config["DB_PATH"]) as conn:
                    if not delete_subscriber_data(conn, subscriber_id):
                        abort(404)
            except Exception:
                app.logger.exception(
                    "admin_subscriber_delete_failed subscriber_public_id=%s",
                    person["public_id"],
                )
                flash("구독자를 삭제하지 못했습니다. 데이터는 변경되지 않았습니다.", "error")
                return redirect(
                    url_for("admin_subscriber_delete", subscriber_id=subscriber_id)
                )
            flash("구독자와 연결된 운영 기록을 삭제했습니다.", "success")
            return redirect(url_for("admin_subscribers"))

        return render_template(
            "admin_subscriber_delete.html", person=person, summary=summary
        )

    @app.get("/admin/episodes/<int:episode_id>/feedback")
    @admin_required
    def admin_feedback(episode_id):
        conn = db()
        episode = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
        response_count = conn.execute("SELECT COUNT(*) FROM feedback_submissions WHERE episode_id=?", (episode_id,)).fetchone()[0]
        questions = conn.execute("SELECT * FROM feedback_questions WHERE episode_id=? ORDER BY display_order,id", (episode_id,)).fetchall()
        summaries = []
        for q in questions:
            answers = conn.execute(
                """SELECT fa.value_text,fa.value_json FROM feedback_answers fa
                   JOIN feedback_submissions fs ON fs.id=fa.submission_id
                   WHERE fs.episode_id=? AND fa.feedback_question_id=? ORDER BY fs.created_at DESC""",
                (episode_id, q["id"]),
            ).fetchall()
            summaries.append((q, feedback_summary(q["response_type"], answers)))
        conn.close()
        if not episode:
            abort(404)
        return render_template("admin_feedback.html", episode=episode, response_count=response_count, summaries=summaries)

    @app.get("/admin/export/<kind>.csv")
    @admin_required
    def admin_export(kind):
        conn = db()
        output = io.StringIO()
        writer = csv.writer(output)
        if kind == "participation":
            writer.writerow(["subscriber_id", "episode", "first_completed_at", "source"])
            rows = conn.execute(
                """SELECT s.public_id,e.code,p.first_completed_at,p.source FROM participation p
                   JOIN subscribers s ON s.id=p.subscriber_id JOIN episodes e ON e.id=p.episode_id
                   ORDER BY p.first_completed_at"""
            ).fetchall()
        elif kind == "attempts":
            writer.writerow(["subscriber_id", "episode", "score", "total_points", "completed_at", "source"])
            rows = conn.execute(
                """SELECT s.public_id,e.code,a.score,a.total_points,a.completed_at,a.source FROM quiz_attempts a
                   JOIN subscribers s ON s.id=a.subscriber_id JOIN episodes e ON e.id=a.episode_id
                   WHERE a.status='completed' ORDER BY a.completed_at"""
            ).fetchall()
        else:
            conn.close()
            abort(404)
        for row in rows:
            writer.writerow(list(row))
        conn.close()
        return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename={kind}.csv"})

    return app


def _safe_next(value):
    if not value or "\\" in value:
        return None
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/") or parsed.path.startswith("//"):
        return None
    return value


def _looks_like_email(value):
    if not value or len(value) > 254 or value.count("@") != 1:
        return False
    local, domain = value.rsplit("@", 1)
    return bool(local and "." in domain and not domain.startswith(".") and not domain.endswith("."))


def _public_magic_link_url(app, raw_token):
    base_url = app.config.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
    parsed = urlsplit(base_url)
    if not base_url or parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise MagicLinkDeliveryError("PUBLIC_BASE_URL is not configured")
    return f"{base_url}{url_for('magic_link_verify')}?token={quote(raw_token, safe='')}"


def create_default_feedback(conn, episode_id):
    definitions = [
        ("이번 오디오레터에서 인상 깊었던 내용", "multi_choice", 1),
        ("이번 오디오레터에 대한 전반적 만족도", "rating", 2),
        ("다음 오디오레터에서 다뤘으면 하는 내용", "text", 3),
    ]
    ids = []
    for prompt, response_type, order in definitions:
        ids.append(conn.execute(
            "INSERT INTO feedback_questions(episode_id,prompt,response_type,display_order) VALUES(?,?,?,?)",
            (episode_id, prompt, response_type, order),
        ).lastrowid)
    for order, label in enumerate(["핵심 개념", "실제 사례", "식단 적용", "보호자 관찰 기준"], start=1):
        conn.execute(
            "INSERT INTO feedback_options(feedback_question_id,label,display_order) VALUES(?,?,?)",
            (ids[0], label, order),
        )
    for order in range(1, 6):
        conn.execute(
            "INSERT INTO feedback_options(feedback_question_id,label,display_order) VALUES(?,?,?)",
            (ids[1], str(order), order),
        )


def seed_demo(db_path):
    with transaction(db_path) as conn:
        season = conn.execute("SELECT id FROM seasons WHERE code='S1'").fetchone()
        season_id = season["id"] if season else conn.execute(
            "INSERT INTO seasons(code,title,is_active,created_at) VALUES('S1','시즌 1',1,?)", (utcnow(),)
        ).lastrowid
        for public_id, name in (("test-alpha", "테스트 구독자 A"), ("test-beta", "테스트 구독자 B")):
            conn.execute(
                "INSERT OR IGNORE INTO subscribers(public_id,display_name,is_test,created_at) VALUES(?,?,1,?)",
                (public_id, name, utcnow()),
            )
        episode = conn.execute("SELECT id FROM episodes WHERE code='R041'").fetchone()
        if episode:
            return
        episode_id = conn.execute(
            """INSERT INTO episodes(season_id,code,title,description,display_order,is_published,created_at,updated_at)
               VALUES(?,?,?,?,?,1,?,?)""",
            (season_id, "R041", "장과 식단을 읽는 법", "3문제 · 약 2분", 41, utcnow(), utcnow()),
        ).lastrowid
        demo_questions = [
            ("이 앱에서 참여로 인정되는 시점은 언제인가요?", ["퀴즈 시작", "최초 완료", "피드백 작성"], 2, "피드백과 관계없이 퀴즈를 처음 완료하면 참여 1회가 기록됩니다."),
            ("같은 회차를 다시 풀면 참여 횟수는 어떻게 되나요?", ["매번 증가", "점수가 오르면 증가", "증가하지 않음"], 3, None),
            ("이해 테스트의 가장 중요한 목적은 무엇인가요?", ["경쟁", "꾸준한 학습 참여", "순위 결정"], 2, "정답률보다 꾸준히 듣고 생각한 기록을 쌓는 데 목적이 있습니다."),
        ]
        for q_order, (text, choices, correct, explanation) in enumerate(demo_questions, start=1):
            qid = conn.execute(
                "INSERT INTO questions(episode_id,text,points,explanation,display_order) VALUES(?,?,?,?,?)",
                (episode_id, text, 1, explanation, q_order),
            ).lastrowid
            for c_order, label in enumerate(choices, start=1):
                conn.execute(
                    "INSERT INTO choices(question_id,text,is_correct,display_order) VALUES(?,?,?,?)",
                    (qid, label, int(c_order == correct), c_order),
                )
        create_default_feedback(conn, episode_id)


if __name__ == "__main__":
    create_app().run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)
