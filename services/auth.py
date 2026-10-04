from __future__ import annotations

import hashlib
import hmac
import secrets as pysecrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from config.secrets import SecretStore

PASSWORD_SECRET = "ADMIN_PASSWORD_HASH"
COOKIE_NAME = "agentos_session"
PBKDF2_ITERATIONS = 150_000


def _utc_iso(delta_seconds: int = 0) -> str:
    moment = datetime.now(timezone.utc) + timedelta(seconds=delta_seconds)
    return moment.isoformat(timespec="seconds")


class AdminAuth:
    """Local admin authentication: password bootstrap, sessions, CSRF, lockout.

    - the password is stored only as a PBKDF2 hash inside the ``SecretStore``;
    - session tokens live in SQLite as SHA-256 hashes (cookie holds the raw);
    - every auth event is written to the audit log;
    - login attempts are rate-limited per client IP (lockout window).
    """

    def __init__(
        self,
        storage: Any,
        secret_store: SecretStore,
        *,
        session_ttl_seconds: int = 3600,
        max_failures: int = 5,
        lockout_seconds: int = 300,
    ) -> None:
        self.storage = storage
        self.secrets = secret_store
        self.session_ttl_seconds = session_ttl_seconds
        self.max_failures = max_failures
        self.lockout_seconds = lockout_seconds
        self._failures: dict[str, list[float]] = {}

    # --- password --------------------------------------------------------

    def password_configured(self) -> bool:
        return self.secrets.has(PASSWORD_SECRET)

    def set_password(self, raw: str) -> None:
        if not isinstance(raw, str) or not 8 <= len(raw) <= 256:
            raise ValueError("password must be 8-256 characters")
        if "\n" in raw or "\r" in raw:
            raise ValueError("password must be a single line")
        salt = pysecrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", raw.encode("utf-8"), salt, PBKDF2_ITERATIONS)
        stored = f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"
        self.secrets.set(PASSWORD_SECRET, stored)
        self.storage.add_audit("admin", "auth.setup")

    def verify_password(self, raw: str) -> bool:
        stored = self.secrets.get(PASSWORD_SECRET)
        if not stored:
            return False
        try:
            algorithm, iterations, salt_hex, digest_hex = stored.split("$")
            if algorithm != "pbkdf2_sha256":
                return False
            candidate = hashlib.pbkdf2_hmac(
                "sha256", raw.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
            )
        except (ValueError, TypeError):
            return False
        return hmac.compare_digest(candidate, bytes.fromhex(digest_hex))

    # --- rate limiting ---------------------------------------------------

    def rate_limited(self, client_ip: str) -> bool:
        now = time.monotonic()
        attempts = [
            stamp
            for stamp in self._failures.get(client_ip, [])
            if now - stamp < self.lockout_seconds
        ]
        self._failures[client_ip] = attempts
        return len(attempts) >= self.max_failures

    def _record_failure(self, client_ip: str) -> None:
        self._failures.setdefault(client_ip, []).append(time.monotonic())

    def _clear_failures(self, client_ip: str) -> None:
        self._failures.pop(client_ip, None)

    # --- sessions --------------------------------------------------------

    @staticmethod
    def _hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def login(
        self, password: str, client_ip: str
    ) -> tuple[str | None, str | None, str]:
        """Returns (session_token, csrf_token, reason)."""
        if not self.password_configured():
            return None, None, "not_configured"
        if self.rate_limited(client_ip):
            return None, None, "rate_limited"
        if not isinstance(password, str) or not self.verify_password(password):
            self._record_failure(client_ip)
            self.storage.add_audit("anonymous", "auth.login_failed", f"ip={client_ip}")
            return None, None, "invalid"
        self._clear_failures(client_ip)
        token = pysecrets.token_urlsafe(32)
        csrf = pysecrets.token_urlsafe(24)
        self.storage.create_session(
            self._hash_token(token), csrf, _utc_iso(self.session_ttl_seconds)
        )
        self.storage.add_audit("admin", "auth.login", f"ip={client_ip}")
        return token, csrf, "ok"

    def session_valid(self, token: str | None) -> dict[str, Any] | None:
        if not token:
            return None
        return self.storage.find_session(self._hash_token(token))

    def logout(self, token: str | None) -> bool:
        if not token:
            return False
        removed = self.storage.delete_session(self._hash_token(token))
        if removed:
            self.storage.add_audit("admin", "auth.logout")
        return removed

    # --- session administration (dashboard) ------------------------------

    def list_sessions(self) -> list[dict[str, Any]]:
        """Active sessions, safe columns only (id, created_at, expires_at)."""
        self.storage.purge_expired_sessions()
        return self.storage.list_sessions()

    def revoke_session(self, session_id: int) -> bool:
        removed = self.storage.delete_session_by_id(session_id)
        if removed:
            self.storage.add_audit("admin", "auth.session_revoke", f"session={session_id}")
        return removed
