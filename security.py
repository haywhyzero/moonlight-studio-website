"""Codes, hashes, and signed entitlement tokens.

Security properties:
* Redemption codes are derived from the order id with a server-side pepper, so
  the service can re-display a paid order's code without storing the plaintext.
* Only an HMAC of the normalized code is stored for lookup.
* Entitlement tokens are compact HMAC-signed payloads. The app treats them as
  opaque; only this service can mint or validate them.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from datetime import datetime, timezone
from typing import Any

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I
CODE_LENGTH = 12
CODE_PREFIX = "ML"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def normalize_code(raw: str) -> str:
    """Strip formatting and the ML prefix, returning 12 symbols."""
    compact = "".join(ch for ch in raw.upper() if ch.isalnum())
    if compact.startswith(CODE_PREFIX):
        compact = compact[len(CODE_PREFIX) :]
    if len(compact) != CODE_LENGTH or any(
        ch not in CODE_ALPHABET for ch in compact
    ):
        raise ValueError("Enter the 12-character code printed after payment.")
    return compact


def format_code(compact: str) -> str:
    groups = [compact[i : i + 4] for i in range(0, CODE_LENGTH, 4)]
    return f"{CODE_PREFIX}-" + "-".join(groups)


def derive_code(order_id: str, pepper: str) -> str:
    digest = hmac.new(
        pepper.encode("utf-8"),
        f"code:{order_id}".encode("utf-8"),
        hashlib.sha256,
    ).digest()
    compact = "".join(CODE_ALPHABET[b % len(CODE_ALPHABET)] for b in digest[:CODE_LENGTH])
    return format_code(compact)


def code_hash(raw_code: str, pepper: str) -> str:
    compact = normalize_code(raw_code)
    return hmac.new(
        pepper.encode("utf-8"),
        f"hash:{compact}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def new_claim_token() -> str:
    return secrets.token_urlsafe(32)


def claim_hash(token: str, pepper: str) -> str:
    return hmac.new(
        pepper.encode("utf-8"),
        f"claim:{token}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def sign_entitlement(payload: dict[str, Any], secret: str) -> str:
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    encoded = _b64(body)
    signature = hmac.new(
        secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256
    ).digest()
    return f"{encoded}.{_b64(signature)}"


def verify_entitlement(token: str, secret: str) -> dict[str, Any]:
    try:
        encoded, signature = token.split(".", 1)
    except ValueError as exc:
        raise ValueError("Malformed entitlement token.") from exc
    expected = hmac.new(
        secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256
    ).digest()
    try:
        supplied = _unb64(signature)
    except Exception as exc:  # noqa: BLE001 - normalize any decoder error
        raise ValueError("Malformed entitlement token.") from exc
    if not hmac.compare_digest(expected, supplied):
        raise ValueError("Invalid entitlement token.")
    try:
        payload = json.loads(_unb64(encoded))
    except Exception as exc:  # noqa: BLE001
        raise ValueError("Malformed entitlement token.") from exc
    if not isinstance(payload, dict):
        raise ValueError("Malformed entitlement token.")
    exp = payload.get("exp")
    if not isinstance(exp, int) or exp < int(time.time()):
        raise ValueError("This subscription has expired.")
    return payload


def constant_time_equals(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
