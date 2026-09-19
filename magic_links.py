import hashlib
import html
import json
import logging
import secrets
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from db import transaction


LOGGER = logging.getLogger(__name__)
BREVO_ENDPOINT = "https://api.brevo.com/v3/smtp/email"


class MagicLinkDeliveryError(RuntimeError):
    pass


def token_digest(raw_token):
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def create_magic_link_token(db_path, subscriber_id, redirect_path, ttl_minutes):
    raw_token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=ttl_minutes)
    with transaction(db_path) as conn:
        conn.execute(
            "UPDATE magic_link_tokens SET used_at=? WHERE subscriber_id=? AND used_at IS NULL",
            (now.isoformat(), subscriber_id),
        )
        conn.execute(
            """INSERT INTO magic_link_tokens
               (subscriber_id,token_hash,redirect_path,created_at,expires_at)
               VALUES(?,?,?,?,?)""",
            (
                subscriber_id,
                token_digest(raw_token),
                redirect_path,
                now.isoformat(),
                expires_at.isoformat(),
            ),
        )
    return raw_token


def consume_magic_link_token(db_path, raw_token):
    if not raw_token:
        return None
    digest = token_digest(raw_token)
    now = datetime.now(timezone.utc).isoformat()
    with transaction(db_path) as conn:
        row = conn.execute(
            """SELECT id,subscriber_id,redirect_path FROM magic_link_tokens
               WHERE token_hash=? AND used_at IS NULL AND expires_at>?""",
            (digest, now),
        ).fetchone()
        if not row:
            return None
        changed = conn.execute(
            "UPDATE magic_link_tokens SET used_at=? WHERE id=? AND used_at IS NULL",
            (now, row["id"]),
        ).rowcount
        if changed != 1:
            return None
        return {
            "subscriber_id": row["subscriber_id"],
            "redirect_path": row["redirect_path"],
        }


def invalidate_magic_link_token(db_path, raw_token):
    if not raw_token:
        return
    with transaction(db_path) as conn:
        conn.execute(
            "UPDATE magic_link_tokens SET used_at=? WHERE token_hash=? AND used_at IS NULL",
            (datetime.now(timezone.utc).isoformat(), token_digest(raw_token)),
        )


def magic_link_on_cooldown(conn, subscriber_id, cooldown_seconds):
    if cooldown_seconds <= 0:
        return False
    latest = conn.execute(
        "SELECT created_at FROM magic_link_tokens WHERE subscriber_id=? ORDER BY id DESC LIMIT 1",
        (subscriber_id,),
    ).fetchone()
    if not latest:
        return False
    try:
        created = datetime.fromisoformat(latest["created_at"])
        return datetime.now(timezone.utc) < created + timedelta(seconds=cooldown_seconds)
    except ValueError:
        return False


def send_magic_link_via_brevo(recipient_email, magic_url, config):
    api_key = config.get("BREVO_API_KEY", "")
    sender_email = config.get("MAGIC_LINK_SENDER_EMAIL", "")
    sender_name = config.get("MAGIC_LINK_SENDER_NAME", "")
    if not api_key or not sender_email or not sender_name:
        raise MagicLinkDeliveryError("Brevo magic-link configuration is incomplete")

    payload = {
        "sender": {"email": sender_email, "name": sender_name},
        "to": [{"email": recipient_email}],
        "subject": "오디오레터 이해 테스트 인증 링크",
        "htmlContent": (
            "<p>반려견영양연구소 이해 테스트 인증 요청입니다.</p>"
            f'<p><a href="{html.escape(magic_url, quote=True)}">이 브라우저에서 인증하기</a></p>'
            "<p>이 링크는 한 번만 사용할 수 있으며 곧 만료됩니다. "
            "본인이 요청하지 않았다면 이 메일을 무시해주세요.</p>"
        ),
    }
    request = urllib.request.Request(
        BREVO_ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "accept": "application/json",
            "api-key": api_key,
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request, timeout=int(config.get("BREVO_TIMEOUT_SECONDS", 10))
        ) as response:
            if response.status < 200 or response.status >= 300:
                raise MagicLinkDeliveryError(f"Brevo returned status {response.status}")
    except urllib.error.HTTPError as exc:
        LOGGER.error("magic_link_delivery_failed provider=brevo status=%s", exc.code)
        raise MagicLinkDeliveryError(f"Brevo returned status {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        LOGGER.error("magic_link_delivery_failed provider=brevo error=%s", type(exc).__name__)
        raise MagicLinkDeliveryError("Brevo request failed") from exc
