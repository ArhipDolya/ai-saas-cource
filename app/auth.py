import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
import time

from dotenv import load_dotenv

from app.database import ENV_PATH


ADMIN_PASSWORD_VARIABLE = "ADMIN_PASSWORD"
TOKEN_TTL_SECONDS = 86_400  # 24 hours session validity.
BEARER_PREFIX = "Bearer "
TOKEN_SEPARATOR = "."
SIGNATURE_DIGEST_SIZE = 32


class AdminAuthConfigError(ValueError):
    """The admin password configuration is missing or invalid."""


def load_admin_password() -> str:
    """Load ADMIN_PASSWORD from the environment or project .env file."""
    load_dotenv(dotenv_path=ENV_PATH)
    admin_password = os.getenv(ADMIN_PASSWORD_VARIABLE, "")
    if not admin_password.strip():
        raise AdminAuthConfigError(
            "Set ADMIN_PASSWORD in the environment or the project .env file.",
        )
    return admin_password


class AdminAuthService:
    """Stateless HMAC-signed session token authentication for the admin API."""

    def __init__(
        self,
        *,
        admin_password: str,
        signing_key: bytes | None = None,
        token_ttl_seconds: int = TOKEN_TTL_SECONDS,
    ) -> None:
        if not admin_password.strip():
            raise AdminAuthConfigError("Admin password must not be empty.")
        self._admin_password_bytes = admin_password.encode("utf-8")
        self._signing_key = signing_key or secrets.token_bytes(SIGNATURE_DIGEST_SIZE)
        self._token_ttl_seconds = token_ttl_seconds

    def verify_password(self, password: str) -> bool:
        """Compare the submitted password against ADMIN_PASSWORD in constant time."""
        submitted_bytes = password.encode("utf-8")
        return hmac.compare_digest(submitted_bytes, self._admin_password_bytes)

    def issue_token(self) -> str:
        """Create a signed expiring bearer token for an authenticated admin."""
        expires_at = int(time.time()) + self._token_ttl_seconds
        payload = {
            "exp": expires_at,
            "nonce": secrets.token_hex(8),
        }
        payload_bytes = json.dumps(
            payload,
            separators=(",", ":"),
        ).encode("utf-8")
        encoded_payload = _encode_base64url(payload_bytes)
        signature = self._sign(encoded_payload.encode("ascii"))
        return f"{encoded_payload}{TOKEN_SEPARATOR}{signature}"

    def verify_token(self, token: str) -> bool:
        """Validate token format, HMAC signature, and expiration timestamp."""
        if TOKEN_SEPARATOR not in token:
            return False

        encoded_payload, encoded_signature = token.split(TOKEN_SEPARATOR, maxsplit=1)
        if not encoded_payload or not encoded_signature:
            return False

        expected_signature = self._sign(encoded_payload.encode("ascii"))
        is_valid_signature = hmac.compare_digest(
            encoded_signature,
            expected_signature,
        )
        if not is_valid_signature:
            return False

        try:
            payload_bytes = _decode_base64url(encoded_payload)
            payload: object = json.loads(payload_bytes.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, binascii.Error):
            return False

        if not isinstance(payload, dict):
            return False

        expires_at = payload.get("exp")
        is_valid_expiry = isinstance(expires_at, int) and not isinstance(
            expires_at,
            bool,
        )
        if not is_valid_expiry:
            return False

        return expires_at > int(time.time())

    def _sign(self, payload: bytes) -> str:
        digest = hmac.new(
            key=self._signing_key,
            msg=payload,
            digestmod=hashlib.sha256,
        ).digest()
        return _encode_base64url(digest)


def extract_bearer_token(authorization_header: str | None) -> str | None:
    """Extract the bearer token from an HTTP Authorization header."""
    if not authorization_header:
        return None
    if not authorization_header.startswith(BEARER_PREFIX):
        return None
    token = authorization_header[len(BEARER_PREFIX) :].strip()
    return token or None


def _encode_base64url(raw_bytes: bytes) -> str:
    return base64.urlsafe_b64encode(raw_bytes).decode("ascii").rstrip("=")


def _decode_base64url(encoded: str) -> bytes:
    padding_length = (-len(encoded)) % 4
    padded = f"{encoded}{'=' * padding_length}"
    return base64.urlsafe_b64decode(padded.encode("ascii"))
