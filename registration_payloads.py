import json

from cryptography.fernet import Fernet, InvalidToken


class RegistrationEncryptionConfigurationError(RuntimeError):
    """Raised when the dedicated registration payload key is not usable."""


class RegistrationPayloadError(RuntimeError):
    """Raised when a stored payload cannot be authenticated or decoded."""


_PAYLOAD_FIELDS = (
    "email",
    "guardian_name",
    "contact_phone",
    "payer_name",
    "interests",
)


def _fernet(encryption_key):
    if not isinstance(encryption_key, str) or not encryption_key.strip():
        raise RegistrationEncryptionConfigurationError(
            "SUBSCRIPTION_REGISTRATION_ENCRYPTION_KEY is not configured"
        )
    try:
        return Fernet(encryption_key.strip().encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise RegistrationEncryptionConfigurationError(
            "SUBSCRIPTION_REGISTRATION_ENCRYPTION_KEY is invalid"
        ) from exc


def encrypt_registration_payload(values, registration_public_id, encryption_key):
    payload = {field: values.get(field) for field in _PAYLOAD_FIELDS}
    payload["registration_id"] = registration_public_id
    plaintext = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return _fernet(encryption_key).encrypt(plaintext).decode("ascii")


def decrypt_registration_payload(token, registration_public_id, encryption_key):
    if not isinstance(token, str) or not token:
        raise RegistrationPayloadError("registration payload is missing")
    try:
        plaintext = _fernet(encryption_key).decrypt(token.encode("ascii"))
        payload = json.loads(plaintext.decode("utf-8"))
    except RegistrationEncryptionConfigurationError:
        raise
    except (InvalidToken, UnicodeEncodeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RegistrationPayloadError(
            "registration payload authentication failed"
        ) from exc

    if not isinstance(payload, dict) or payload.get("registration_id") != registration_public_id:
        raise RegistrationPayloadError("registration payload does not match its registration")
    if any(field not in payload for field in _PAYLOAD_FIELDS):
        raise RegistrationPayloadError("registration payload is incomplete")
    return {field: payload[field] for field in _PAYLOAD_FIELDS}
