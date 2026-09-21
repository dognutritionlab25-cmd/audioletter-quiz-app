from urllib.parse import urlsplit

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for

from auth import admin_required, subscriber_required
from db import connect, transaction, utcnow


resources_bp = Blueprint("resources", __name__)


def _db():
    return connect(current_app.config["DB_PATH"])


def _normalize_external_url(value):
    value = (value or "").strip()
    if not value:
        return None
    if any(character in value for character in ("\r", "\n", "\t", "\\")):
        raise ValueError("외부 링크는 올바른 http 또는 https 주소여야 합니다.")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("외부 링크는 올바른 http 또는 https 주소여야 합니다.")
    return value


def _form_values():
    values = {
        "title": request.form.get("title", "").strip(),
        "body": request.form.get("body", "").strip(),
        "category": request.form.get("category", "").strip(),
        "external_url": request.form.get("external_url", "").strip(),
        "is_published": int("is_published" in request.form),
    }
    errors = []
    if not values["title"]:
        errors.append("제목을 입력해주세요.")
    elif len(values["title"]) > 200:
        errors.append("제목은 200자 이하로 입력해주세요.")
    if not values["category"]:
        errors.append("카테고리를 입력해주세요.")
    elif len(values["category"]) > 100:
        errors.append("카테고리는 100자 이하로 입력해주세요.")
    if not values["body"]:
        errors.append("본문을 입력해주세요.")
    try:
        values["external_url"] = _normalize_external_url(values["external_url"])
    except ValueError as exc:
        errors.append(str(exc))
    return values, errors


@resources_bp.get("/resources")
@subscriber_required
def resource_list():
    conn = _db()
    rows = conn.execute(
        """SELECT id,title,category,created_at
           FROM resources WHERE is_published=1
           ORDER BY created_at DESC,id DESC"""
    ).fetchall()
    conn.close()
    return render_template("resources_list.html", resources=rows)


@resources_bp.get("/resources/<int:resource_id>")
@subscriber_required
def resource_detail(resource_id):
    conn = _db()
    resource = conn.execute(
        """SELECT * FROM resources
           WHERE id=? AND is_published=1""",
        (resource_id,),
    ).fetchone()
    conn.close()
    if not resource:
        abort(404)
    return render_template("resource_detail.html", resource=resource)


@resources_bp.get("/admin/resources")
@admin_required
def admin_resource_list():
    conn = _db()
    rows = conn.execute(
        """SELECT * FROM resources
           ORDER BY created_at DESC,id DESC"""
    ).fetchall()
    conn.close()
    return render_template("admin_resources.html", resources=rows)


@resources_bp.route("/admin/resources/new", methods=["GET", "POST"])
@admin_required
def admin_resource_new():
    resource = None
    if request.method == "POST":
        values, errors = _form_values()
        if errors:
            for error in errors:
                flash(error, "error")
            resource = values
        else:
            now = utcnow()
            with transaction(current_app.config["DB_PATH"]) as conn:
                resource_id = conn.execute(
                    """INSERT INTO resources
                       (title,body,category,external_url,is_published,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (
                        values["title"], values["body"], values["category"],
                        values["external_url"], values["is_published"], now, now,
                    ),
                ).lastrowid
            flash("자료를 등록했습니다.", "success")
            return redirect(url_for("resources.admin_resource_edit", resource_id=resource_id))
    return render_template("admin_resource_form.html", resource=resource)


@resources_bp.route("/admin/resources/<int:resource_id>/edit", methods=["GET", "POST"])
@admin_required
def admin_resource_edit(resource_id):
    conn = _db()
    stored_resource = conn.execute(
        "SELECT * FROM resources WHERE id=?", (resource_id,)
    ).fetchone()
    conn.close()
    if not stored_resource:
        abort(404)

    resource = stored_resource
    if request.method == "POST":
        values, errors = _form_values()
        if errors:
            for error in errors:
                flash(error, "error")
            resource = values
        else:
            with transaction(current_app.config["DB_PATH"]) as conn:
                conn.execute(
                    """UPDATE resources
                       SET title=?,body=?,category=?,external_url=?,is_published=?,updated_at=?
                       WHERE id=?""",
                    (
                        values["title"], values["body"], values["category"],
                        values["external_url"], values["is_published"], utcnow(), resource_id,
                    ),
                )
            flash("자료를 저장했습니다.", "success")
            return redirect(url_for("resources.admin_resource_edit", resource_id=resource_id))
    return render_template(
        "admin_resource_form.html", resource=resource, resource_id=resource_id
    )


@resources_bp.post("/admin/resources/<int:resource_id>/delete")
@admin_required
def admin_resource_delete(resource_id):
    with transaction(current_app.config["DB_PATH"]) as conn:
        resource = conn.execute(
            "SELECT id FROM resources WHERE id=?", (resource_id,)
        ).fetchone()
        if not resource:
            abort(404)
        conn.execute("DELETE FROM resources WHERE id=?", (resource_id,))
    flash("자료를 삭제했습니다.", "success")
    return redirect(url_for("resources.admin_resource_list"))
