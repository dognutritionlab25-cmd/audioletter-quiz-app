from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from auth import admin_required, current_subscriber_id, paid_subscriber_required
from db import connect, transaction, utcnow
from magic_links import send_community_post_notification_via_brevo
from presenters import format_korean_datetime


community_bp = Blueprint("community", __name__)


def _db():
    return connect(current_app.config["DB_PATH"])


def _post_values():
    values = {
        "title": request.form.get("title", "").strip(),
        "body": request.form.get("body", "").strip(),
    }
    errors = []
    if not values["title"]:
        errors.append("제목을 입력해주세요.")
    elif len(values["title"]) > 200:
        errors.append("제목은 200자 이하로 입력해주세요.")
    if not values["body"]:
        errors.append("본문을 입력해주세요.")
    elif len(values["body"]) > 20000:
        errors.append("본문은 20,000자 이하로 입력해주세요.")
    return values, errors


def _notify_admin(post_id, title, author_name, created_at):
    path = url_for("community.admin_post_detail", post_id=post_id)
    base_url = current_app.config.get("PUBLIC_BASE_URL", "").rstrip("/")
    admin_url = f"{base_url}{path}" if base_url else url_for(
        "community.admin_post_detail", post_id=post_id, _external=True
    )
    sender = (
        current_app.config.get("COMMUNITY_NOTIFICATION_SENDER")
        or send_community_post_notification_via_brevo
    )
    try:
        sender(
            title,
            author_name,
            format_korean_datetime(created_at),
            admin_url,
            current_app.config,
        )
    except Exception as exc:
        current_app.logger.error(
            "community_post_notification_failed error=%s post_id=%s",
            type(exc).__name__,
            post_id,
        )


@community_bp.get("/community")
@paid_subscriber_required
def post_list():
    conn = _db()
    posts = conn.execute(
        """SELECT p.id,p.title,p.created_at,
                  COALESCE(s.display_name,'구독자') author_name,
                  (SELECT COUNT(*) FROM community_comments c WHERE c.post_id=p.id) comment_count,
                  (SELECT COUNT(*) FROM community_likes l WHERE l.post_id=p.id) like_count
           FROM community_posts p
           JOIN subscribers s ON s.id=p.subscriber_id
           WHERE p.is_visible=1
           ORDER BY p.created_at DESC,p.id DESC"""
    ).fetchall()
    conn.close()
    return render_template("community_list.html", posts=posts)


@community_bp.route("/community/new", methods=["GET", "POST"])
@paid_subscriber_required
def post_new():
    post = None
    if request.method == "POST":
        values, errors = _post_values()
        post = values
        if errors:
            for error in errors:
                flash(error, "error")
        else:
            created_at = utcnow()
            with transaction(current_app.config["DB_PATH"]) as conn:
                subscriber = conn.execute(
                    "SELECT display_name FROM subscribers WHERE id=?",
                    (current_subscriber_id(),),
                ).fetchone()
                post_id = conn.execute(
                    """INSERT INTO community_posts
                       (subscriber_id,title,body,is_visible,created_at,updated_at)
                       VALUES(?,?,?,1,?,?)""",
                    (
                        current_subscriber_id(),
                        values["title"],
                        values["body"],
                        created_at,
                        created_at,
                    ),
                ).lastrowid
            _notify_admin(
                post_id,
                values["title"],
                subscriber["display_name"] or "구독자",
                created_at,
            )
            flash("게시글을 등록했습니다.", "success")
            return redirect(url_for("community.post_detail", post_id=post_id))
    return render_template("community_form.html", post=post, mode="new")


@community_bp.get("/community/<int:post_id>")
@paid_subscriber_required
def post_detail(post_id):
    conn = _db()
    post = conn.execute(
        """SELECT p.*,COALESCE(s.display_name,'구독자') author_name,
                  (SELECT COUNT(*) FROM community_likes l WHERE l.post_id=p.id) like_count,
                  EXISTS(SELECT 1 FROM community_likes l
                         WHERE l.post_id=p.id AND l.subscriber_id=?) liked_by_me
           FROM community_posts p
           JOIN subscribers s ON s.id=p.subscriber_id
           WHERE p.id=? AND p.is_visible=1""",
        (current_subscriber_id(), post_id),
    ).fetchone()
    if not post:
        conn.close()
        abort(404)
    comments = conn.execute(
        """SELECT c.*,COALESCE(s.display_name,'구독자') author_name
           FROM community_comments c
           JOIN subscribers s ON s.id=c.subscriber_id
           WHERE c.post_id=? ORDER BY c.created_at,c.id""",
        (post_id,),
    ).fetchall()
    conn.close()
    return render_template(
        "community_detail.html",
        post=post,
        comments=comments,
        subscriber_id=current_subscriber_id(),
    )


@community_bp.route("/community/<int:post_id>/edit", methods=["GET", "POST"])
@paid_subscriber_required
def post_edit(post_id):
    conn = _db()
    stored_post = conn.execute(
        "SELECT * FROM community_posts WHERE id=? AND is_visible=1", (post_id,)
    ).fetchone()
    conn.close()
    if not stored_post:
        abort(404)
    if stored_post["subscriber_id"] != current_subscriber_id():
        abort(403)

    post = stored_post
    if request.method == "POST":
        values, errors = _post_values()
        post = values
        if errors:
            for error in errors:
                flash(error, "error")
        else:
            with transaction(current_app.config["DB_PATH"]) as conn:
                conn.execute(
                    """UPDATE community_posts SET title=?,body=?,updated_at=?
                       WHERE id=? AND subscriber_id=? AND is_visible=1""",
                    (
                        values["title"],
                        values["body"],
                        utcnow(),
                        post_id,
                        current_subscriber_id(),
                    ),
                )
            flash("게시글을 수정했습니다.", "success")
            return redirect(url_for("community.post_detail", post_id=post_id))
    return render_template("community_form.html", post=post, mode="edit")


@community_bp.post("/community/<int:post_id>/delete")
@paid_subscriber_required
def post_delete(post_id):
    with transaction(current_app.config["DB_PATH"]) as conn:
        post = conn.execute(
            """SELECT subscriber_id FROM community_posts
               WHERE id=? AND is_visible=1""",
            (post_id,),
        ).fetchone()
        if not post:
            abort(404)
        if post["subscriber_id"] != current_subscriber_id():
            abort(403)
        conn.execute("DELETE FROM community_posts WHERE id=?", (post_id,))
    flash("게시글을 삭제했습니다.", "success")
    return redirect(url_for("community.post_list"))


@community_bp.post("/community/<int:post_id>/comments")
@paid_subscriber_required
def comment_create(post_id):
    body = request.form.get("body", "").strip()
    if not body or len(body) > 5000:
        flash("댓글은 1자 이상 5,000자 이하로 입력해주세요.", "error")
        return redirect(url_for("community.post_detail", post_id=post_id))
    with transaction(current_app.config["DB_PATH"]) as conn:
        post = conn.execute(
            "SELECT id FROM community_posts WHERE id=? AND is_visible=1", (post_id,)
        ).fetchone()
        if not post:
            abort(404)
        conn.execute(
            """INSERT INTO community_comments(post_id,subscriber_id,body,created_at)
               VALUES(?,?,?,?)""",
            (post_id, current_subscriber_id(), body, utcnow()),
        )
    return redirect(url_for("community.post_detail", post_id=post_id))


@community_bp.post("/community/comments/<int:comment_id>/delete")
@paid_subscriber_required
def comment_delete(comment_id):
    with transaction(current_app.config["DB_PATH"]) as conn:
        comment = conn.execute(
            """SELECT c.post_id,c.subscriber_id
               FROM community_comments c
               JOIN community_posts p ON p.id=c.post_id
               WHERE c.id=? AND p.is_visible=1""",
            (comment_id,),
        ).fetchone()
        if not comment:
            abort(404)
        if comment["subscriber_id"] != current_subscriber_id():
            abort(403)
        conn.execute("DELETE FROM community_comments WHERE id=?", (comment_id,))
    return redirect(url_for("community.post_detail", post_id=comment["post_id"]))


@community_bp.post("/community/<int:post_id>/like")
@paid_subscriber_required
def like_toggle(post_id):
    with transaction(current_app.config["DB_PATH"]) as conn:
        post = conn.execute(
            "SELECT id FROM community_posts WHERE id=? AND is_visible=1", (post_id,)
        ).fetchone()
        if not post:
            abort(404)
        existing = conn.execute(
            """SELECT id FROM community_likes
               WHERE post_id=? AND subscriber_id=?""",
            (post_id, current_subscriber_id()),
        ).fetchone()
        if existing:
            conn.execute("DELETE FROM community_likes WHERE id=?", (existing["id"],))
        else:
            conn.execute(
                """INSERT INTO community_likes(post_id,subscriber_id,created_at)
                   VALUES(?,?,?)""",
                (post_id, current_subscriber_id(), utcnow()),
            )
    return redirect(url_for("community.post_detail", post_id=post_id))


@community_bp.get("/admin/community")
@admin_required
def admin_post_list():
    conn = _db()
    posts = conn.execute(
        """SELECT p.id,p.title,p.is_visible,p.created_at,
                  COALESCE(s.display_name,'구독자') author_name,
                  (SELECT COUNT(*) FROM community_comments c WHERE c.post_id=p.id) comment_count,
                  (SELECT COUNT(*) FROM community_likes l WHERE l.post_id=p.id) like_count
           FROM community_posts p
           JOIN subscribers s ON s.id=p.subscriber_id
           ORDER BY p.created_at DESC,p.id DESC"""
    ).fetchall()
    conn.close()
    return render_template("admin_community_list.html", posts=posts)


@community_bp.get("/admin/community/<int:post_id>")
@admin_required
def admin_post_detail(post_id):
    conn = _db()
    post = conn.execute(
        """SELECT p.*,COALESCE(s.display_name,'구독자') author_name,
                  (SELECT COUNT(*) FROM community_likes l WHERE l.post_id=p.id) like_count
           FROM community_posts p
           JOIN subscribers s ON s.id=p.subscriber_id
           WHERE p.id=?""",
        (post_id,),
    ).fetchone()
    if not post:
        conn.close()
        abort(404)
    comments = conn.execute(
        """SELECT c.*,COALESCE(s.display_name,'구독자') author_name
           FROM community_comments c
           JOIN subscribers s ON s.id=c.subscriber_id
           WHERE c.post_id=? ORDER BY c.created_at,c.id""",
        (post_id,),
    ).fetchall()
    conn.close()
    return render_template(
        "admin_community_detail.html", post=post, comments=comments
    )


@community_bp.post("/admin/community/<int:post_id>/visibility")
@admin_required
def admin_post_visibility(post_id):
    value = request.form.get("is_visible")
    if value not in {"0", "1"}:
        abort(400)
    with transaction(current_app.config["DB_PATH"]) as conn:
        if conn.execute(
            "SELECT id FROM community_posts WHERE id=?", (post_id,)
        ).fetchone() is None:
            abort(404)
        conn.execute(
            "UPDATE community_posts SET is_visible=?,updated_at=? WHERE id=?",
            (int(value), utcnow(), post_id),
        )
    flash("게시글 공개 상태를 변경했습니다.", "success")
    return redirect(url_for("community.admin_post_detail", post_id=post_id))


@community_bp.post("/admin/community/<int:post_id>/delete")
@admin_required
def admin_post_delete(post_id):
    with transaction(current_app.config["DB_PATH"]) as conn:
        if conn.execute(
            "SELECT id FROM community_posts WHERE id=?", (post_id,)
        ).fetchone() is None:
            abort(404)
        conn.execute("DELETE FROM community_posts WHERE id=?", (post_id,))
    flash("게시글을 삭제했습니다.", "success")
    return redirect(url_for("community.admin_post_list"))


@community_bp.post("/admin/community/comments/<int:comment_id>/delete")
@admin_required
def admin_comment_delete(comment_id):
    with transaction(current_app.config["DB_PATH"]) as conn:
        comment = conn.execute(
            "SELECT post_id FROM community_comments WHERE id=?", (comment_id,)
        ).fetchone()
        if not comment:
            abort(404)
        conn.execute("DELETE FROM community_comments WHERE id=?", (comment_id,))
    flash("댓글을 삭제했습니다.", "success")
    return redirect(url_for("community.admin_post_detail", post_id=comment["post_id"]))
