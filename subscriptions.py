import re
import secrets
from datetime import date

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, session, url_for

from auth import current_subscriber_id
from db import connect, transaction, utcnow
from registration_payloads import (
    RegistrationEncryptionConfigurationError,
    RegistrationPayloadError,
    decrypt_registration_payload,
    encrypt_registration_payload,
)
from services import email_hash


subscriptions_bp = Blueprint("subscriptions", __name__)

PLAN_LABELS = {
    "three-month": "3개월",
    "one-month": "1개월",
}
REGISTRATION_TYPE_LABELS = {
    "new": "신규",
    "renewal": "재구독",
}


@subscriptions_bp.after_request
def protect_registration_responses(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


def _registration_available():
    conn = connect(current_app.config["DB_PATH"])
    settings = conn.execute(
        "SELECT subscription_page_enabled FROM portal_settings WHERE id=1"
    ).fetchone()
    conn.close()
    return bool(settings and settings["subscription_page_enabled"]) or bool(
        session.get("is_admin")
    )


def _looks_like_email(value):
    if not value or len(value) > 254 or value.count("@") != 1:
        return False
    local, domain = value.rsplit("@", 1)
    return bool(local and "." in domain and not domain.startswith(".") and not domain.endswith("."))


def _normalize_phone(value):
    digits = re.sub(r"\D", "", value or "")
    if digits.startswith("82") and len(digits) in {11, 12}:
        digits = "0" + digits[2:]
    if len(digits) not in {10, 11} or not digits.startswith("0"):
        raise ValueError("보호자 연락처를 올바르게 입력해주세요.")
    return digits


def _clean_text(field_name, label, *, required=False, max_length=100):
    value = request.form.get(field_name, "").strip()
    if required and not value:
        raise ValueError(f"{label}을(를) 입력해주세요.")
    if len(value) > max_length:
        raise ValueError(f"{label}은(는) {max_length}자 이하로 입력해주세요.")
    return value or None


def _form_values():
    errors = []
    values = {}
    for field_name, label, required, max_length in (
        ("guardian_name", "보호자 이름", True, 100),
        ("dog_name", "반려견 이름", True, 100),
        ("dog_breed", "반려견 견종", False, 100),
        ("payer_name", "결제자 이름", False, 100),
        ("interests", "관심 내용", False, 1000),
    ):
        try:
            values[field_name] = _clean_text(
                field_name, label, required=required, max_length=max_length
            )
        except ValueError as exc:
            errors.append(str(exc))

    values["email"] = request.form.get("email", "").strip().lower()
    if not _looks_like_email(values["email"]):
        errors.append("이메일 주소를 올바르게 입력해주세요.")

    try:
        values["contact_phone"] = _normalize_phone(
            request.form.get("contact_phone", "")
        )
    except ValueError as exc:
        values["contact_phone"] = request.form.get("contact_phone", "").strip()
        errors.append(str(exc))

    values["plan_code"] = request.form.get("plan_code", "")
    if values["plan_code"] not in PLAN_LABELS:
        errors.append("구독 이용권을 선택해주세요.")

    values["registration_type"] = request.form.get("registration_type", "")
    if values["registration_type"] not in REGISTRATION_TYPE_LABELS:
        errors.append("신규 또는 재구독을 선택해주세요.")

    values["dog_birth_date"] = request.form.get("dog_birth_date", "").strip() or None
    if values["dog_birth_date"]:
        try:
            parsed_birth_date = date.fromisoformat(values["dog_birth_date"])
            if parsed_birth_date > date.today():
                raise ValueError
        except ValueError:
            errors.append("반려견 생일은 오늘 이전의 올바른 날짜여야 합니다.")

    values["privacy_agree"] = request.form.get("privacy_agree", "")
    if values["privacy_agree"] != "yes":
        errors.append("개인정보 수집·이용에 동의해주세요.")

    return values, errors


def _matching_subscriber_id(conn, digest):
    subscriber_id = current_subscriber_id()
    if subscriber_id is None:
        return None
    row = conn.execute(
        """SELECT id FROM subscribers
           WHERE id=? AND email_hash=? AND is_test=0 AND is_active=1""",
        (subscriber_id, digest),
    ).fetchone()
    return row["id"] if row else None


def _save_registration(values):
    now = utcnow()
    digest = email_hash(values["email"], current_app.config["MIGRATION_HASH_SECRET"])
    generated_public_id = f"reg_{secrets.token_urlsafe(12)}"
    with transaction(current_app.config["DB_PATH"]) as conn:
        subscriber_id = _matching_subscriber_id(conn, digest)
        registration_columns = {
            row["name"] for row in conn.execute(
                "PRAGMA table_info(subscription_registrations)"
            )
        }
        legacy_plaintext_columns = {
            "email", "guardian_name", "contact_phone", "payer_name", "interests"
        }
        if registration_columns & legacy_plaintext_columns:
            conn.execute(
                """INSERT INTO subscription_registrations
                   (public_id,subscriber_id,email_hash,email,guardian_name,contact_phone,
                    payer_name,plan_code,registration_type,interests,privacy_agreed_at,
                    status,created_at,updated_at)
                   VALUES(?,?,?,'','','',NULL,?,?,NULL,?,'pending',?,?)
                   ON CONFLICT(email_hash) WHERE status='pending' DO UPDATE SET
                     subscriber_id=COALESCE(excluded.subscriber_id,subscription_registrations.subscriber_id),
                     email='',guardian_name='',contact_phone='',payer_name=NULL,interests=NULL,
                     plan_code=excluded.plan_code,
                     registration_type=excluded.registration_type,
                     privacy_agreed_at=excluded.privacy_agreed_at,
                     updated_at=excluded.updated_at""",
                (
                    generated_public_id,
                    subscriber_id,
                    digest,
                    values["plan_code"],
                    values["registration_type"],
                    now,
                    now,
                    now,
                ),
            )
        else:
            conn.execute(
                """INSERT INTO subscription_registrations
               (public_id,subscriber_id,email_hash,plan_code,registration_type,
                privacy_agreed_at,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,'pending',?,?)
               ON CONFLICT(email_hash) WHERE status='pending' DO UPDATE SET
                 subscriber_id=COALESCE(excluded.subscriber_id,subscription_registrations.subscriber_id),
                 plan_code=excluded.plan_code,
                 registration_type=excluded.registration_type,
                 privacy_agreed_at=excluded.privacy_agreed_at,
                 updated_at=excluded.updated_at""",
                (
                    generated_public_id,
                    subscriber_id,
                    digest,
                    values["plan_code"],
                    values["registration_type"],
                    now,
                    now,
                    now,
                ),
            )
        registration = conn.execute(
            """SELECT id,public_id,subscriber_id FROM subscription_registrations
               WHERE email_hash=? AND status='pending'""",
            (digest,),
        ).fetchone()
        encrypted_payload = encrypt_registration_payload(
            values,
            registration["public_id"],
            current_app.config.get("SUBSCRIPTION_REGISTRATION_ENCRYPTION_KEY", ""),
        )
        conn.execute(
            """INSERT INTO subscription_registration_payloads
               (registration_id,encrypted_payload,created_at,updated_at)
               VALUES(?,?,?,?)
               ON CONFLICT(registration_id) DO UPDATE SET
                 encrypted_payload=excluded.encrypted_payload,
                 updated_at=excluded.updated_at""",
            (registration["id"], encrypted_payload, now, now),
        )
        conn.execute(
            """INSERT INTO dog_profiles
               (subscriber_id,registration_id,name,birth_date,breed,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(registration_id) DO UPDATE SET
                 subscriber_id=COALESCE(excluded.subscriber_id,dog_profiles.subscriber_id),
                 name=excluded.name,
                 birth_date=excluded.birth_date,
                 breed=excluded.breed,
                 updated_at=excluded.updated_at""",
            (
                registration["subscriber_id"],
                registration["id"],
                values["dog_name"],
                values["dog_birth_date"],
                values["dog_breed"],
                now,
                now,
            ),
        )
        return registration["public_id"]


def _api_authorized():
    configured_key = current_app.config.get("SUBSCRIPTION_REGISTRATION_API_KEY", "")
    if not configured_key:
        return False, ({"error": "subscription registration API is not configured"}, 503)
    authorization = request.headers.get("Authorization", "")
    scheme, separator, supplied_key = authorization.partition(" ")
    if (
        not separator
        or scheme.lower() != "bearer"
        or not supplied_key
        or not secrets.compare_digest(supplied_key, configured_key)
    ):
        return False, ({"error": "unauthorized"}, 401)
    return True, None


@subscriptions_bp.route("/subscription/register", methods=["GET", "POST"])
def register():
    if not _registration_available():
        return render_template("subscription_registration_unavailable.html"), 403

    values = {
        "plan_code": request.args.get("plan", "") if request.method == "GET" else "",
    }
    if values["plan_code"] not in PLAN_LABELS:
        values["plan_code"] = ""
    if request.method == "POST":
        values, errors = _form_values()
        if errors:
            for error in errors:
                flash(error, "error")
        else:
            try:
                _save_registration(values)
            except RegistrationEncryptionConfigurationError:
                current_app.logger.error(
                    "subscription_registration_failed reason=encryption_configuration"
                )
                return render_template("subscription_registration_unavailable.html"), 503
            return redirect(url_for("subscriptions.registration_complete"))
    return render_template(
        "subscription_registration.html", values=values, plans=PLAN_LABELS
    )


@subscriptions_bp.get("/subscription/register/complete")
def registration_complete():
    if not _registration_available():
        return render_template("subscription_registration_unavailable.html"), 403
    return render_template("subscription_registration_complete.html")


@subscriptions_bp.get("/api/subscription-registrations/pending")
def pending_registrations_api():
    authorized, error = _api_authorized()
    if not authorized:
        return error
    limit = request.args.get("limit", default=20, type=int)
    if limit is None or limit < 1 or limit > 50:
        return {"error": "limit must be between 1 and 50"}, 400
    conn = connect(current_app.config["DB_PATH"])
    rows = conn.execute(
        """SELECT r.public_id,r.plan_code,r.registration_type,r.created_at,
                  p.encrypted_payload,
                  d.name dog_name,d.birth_date dog_birth_date,d.breed dog_breed
           FROM subscription_registrations r
           LEFT JOIN subscription_registration_payloads p ON p.registration_id=r.id
           JOIN dog_profiles d ON d.registration_id=r.id
           WHERE r.status='pending'
           ORDER BY r.created_at,r.id LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    registrations = []
    for row in rows:
        try:
            payload = decrypt_registration_payload(
                row["encrypted_payload"],
                row["public_id"],
                current_app.config.get("SUBSCRIPTION_REGISTRATION_ENCRYPTION_KEY", ""),
            )
        except RegistrationEncryptionConfigurationError:
            current_app.logger.error(
                "pending_registration_read_failed reason=encryption_configuration"
            )
            return {"error": "registration payload encryption is not configured"}, 503
        except RegistrationPayloadError:
            current_app.logger.error(
                "pending_registration_read_failed reason=payload_unavailable registration_id=%s",
                row["public_id"],
            )
            return {"error": "registration payload unavailable"}, 503
        birth_date = row["dog_birth_date"]
        registrations.append(
            {
                "registration_id": row["public_id"],
                "submitted_at": row["created_at"],
                "subscription_period": PLAN_LABELS[row["plan_code"]],
                "guardian_name": payload["guardian_name"],
                "dog_name": row["dog_name"],
                "email": payload["email"],
                "contact_phone": payload["contact_phone"],
                "dog_birth_date": birth_date.replace("-", ".") if birth_date else None,
                "dog_breed": row["dog_breed"],
                "payer_name": payload["payer_name"],
                "interests": payload["interests"],
                "registration_type": REGISTRATION_TYPE_LABELS[row["registration_type"]],
                "privacy_agreed": True,
            }
        )
    response = current_app.json.response({"registrations": registrations})
    response.headers["Cache-Control"] = "no-store"
    return response


@subscriptions_bp.post("/api/subscription-registrations/<public_id>/complete")
def complete_registration_api(public_id):
    authorized, error = _api_authorized()
    if not authorized:
        return error
    with transaction(current_app.config["DB_PATH"]) as conn:
        registration = conn.execute(
            """SELECT id,status,subscriber_id FROM subscription_registrations
               WHERE public_id=?""",
            (public_id,),
        ).fetchone()
        if not registration:
            return {"error": "registration not found"}, 404
        if registration["subscriber_id"] is None:
            return {"error": "subscriber sync must complete first"}, 409
        if registration["status"] == "completed":
            conn.execute(
                "DELETE FROM subscription_registration_payloads WHERE registration_id=?",
                (registration["id"],),
            )
            return {"status": "unchanged", "registration_id": public_id}, 200
        if registration["status"] != "pending":
            return {"error": "registration cannot be completed"}, 409
        now = utcnow()
        conn.execute(
            """UPDATE subscription_registrations
               SET status='completed',completed_at=?,updated_at=? WHERE id=?""",
            (now, now, registration["id"]),
        )
        conn.execute(
            "DELETE FROM subscription_registration_payloads WHERE registration_id=?",
            (registration["id"],),
        )
    return {"status": "completed", "registration_id": public_id}, 200
