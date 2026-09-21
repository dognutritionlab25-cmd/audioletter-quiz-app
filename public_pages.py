from datetime import date

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, session, url_for

from auth import admin_required
from db import connect, transaction, utcnow


public_pages_bp = Blueprint("public_pages", __name__)

# Keep external checkout destinations in one server-side location. Templates
# receive only internal payment routes, never the PayApp destinations.
PAYMENT_PLANS = {
    "one-month": {
        "name": "1개월 이용권",
        "price": "9,900원",
        "episodes": "4회차 제공",
        "url": "https://www.payapp.kr/L/z49rzA",
    },
    "three-month": {
        "name": "3개월 이용권",
        "price": "25,000원",
        "episodes": "12회차 제공",
        "url": "https://www.payapp.kr/L/z49suD",
    },
}


def _settings():
    conn = connect(current_app.config["DB_PATH"])
    row = conn.execute("SELECT * FROM portal_settings WHERE id=1").fetchone()
    conn.close()
    if row is None:  # Defensive fallback for an incompletely initialized DB.
        raise RuntimeError("portal_settings has not been initialized")
    return row


def _effective_date(value):
    if not value:
        return "확정 전"
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return value
    return f"{parsed.year}년 {parsed.month}월 {parsed.day}일"


def _optional_iso_date(field_name, label):
    value = request.form.get(field_name, "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError(f"{label}은 올바른 날짜여야 합니다.") from exc


@public_pages_bp.get("/subscribe")
def subscribe():
    settings = _settings()
    admin_preview = bool(session.get("is_admin")) and not settings["subscription_page_enabled"]
    payment_available = bool(settings["subscription_page_enabled"]) or bool(
        session.get("is_admin")
    )
    return render_template(
        "subscribe.html",
        plans=PAYMENT_PLANS,
        payment_available=payment_available,
        admin_preview=admin_preview,
    )


@public_pages_bp.post("/subscribe/pay/<plan_code>")
def start_payment(plan_code):
    plan = PAYMENT_PLANS.get(plan_code)
    if plan is None:
        abort(404)

    settings = _settings()
    payment_available = bool(settings["subscription_page_enabled"]) or bool(
        session.get("is_admin")
    )
    if not payment_available:
        abort(403)

    if request.form.get("agree_terms") != "yes" or request.form.get("agree_privacy") != "yes":
        flash("이용약관과 개인정보처리방침에 모두 동의해야 결제를 진행할 수 있습니다.", "error")
        return redirect(url_for("public_pages.subscribe"))

    return redirect(plan["url"], code=303)


@public_pages_bp.get("/terms")
def terms():
    return render_template(
        "terms.html", effective_date=_effective_date(_settings()["terms_effective_date"])
    )


@public_pages_bp.get("/privacy")
def privacy():
    return render_template(
        "privacy.html",
        effective_date=_effective_date(_settings()["privacy_effective_date"]),
    )


@public_pages_bp.route("/admin/portal-settings", methods=["GET", "POST"])
@admin_required
def admin_portal_settings():
    if request.method == "POST":
        try:
            terms_date = _optional_iso_date("terms_effective_date", "이용약관 시행일")
            privacy_date = _optional_iso_date("privacy_effective_date", "개인정보처리방침 시행일")
        except ValueError as exc:
            flash(str(exc), "error")
        else:
            enabled = int(request.form.get("subscription_page_enabled") == "1")
            with transaction(current_app.config["DB_PATH"]) as conn:
                conn.execute(
                    """UPDATE portal_settings
                       SET subscription_page_enabled=?,terms_effective_date=?,
                           privacy_effective_date=?,updated_at=?
                       WHERE id=1""",
                    (enabled, terms_date, privacy_date, utcnow()),
                )
            flash("Portal 공개 설정을 저장했습니다.", "success")
            return redirect(url_for("public_pages.admin_portal_settings"))

    return render_template("admin_portal_settings.html", settings=_settings())
