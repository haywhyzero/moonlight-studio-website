"""Business logic for checkout fulfilment, activation, and usage quotas."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from typing import Any

try:
    from .config import Settings
    from .db import Database
    from .payments import PaymentError, PaymentVerification, build_payment_provider
    from .plans import PLAN_TERMS, TERMS, Plan, add_months, get_plan
    from .security import (
        claim_hash,
        code_hash,
        constant_time_equals,
        derive_code,
        format_code,
        iso,
        new_claim_token,
        normalize_code,
        parse_iso,
        sign_entitlement,
        utcnow,
        verify_entitlement,
    )
    from .email_service import send_receipt_email
except (ImportError, ValueError):
    from config import Settings
    from db import Database
    from payments import PaymentError, PaymentVerification, build_payment_provider
    from plans import PLAN_TERMS, TERMS, Plan, add_months, get_plan
    from security import (
        claim_hash,
        code_hash,
        constant_time_equals,
        derive_code,
        format_code,
        iso,
        new_claim_token,
        normalize_code,
        parse_iso,
        sign_entitlement,
        utcnow,
        verify_entitlement,
    )
    from email_service import send_receipt_email

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class LicenseError(Exception):
    def __init__(self, message: str, *, status_code: int = 400, code: str = "bad_request"):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class LicenseService:
    def __init__(self, database: Database, settings: Settings):
        self.db = database
        self.settings = settings

    def check_rate_limit(
        self,
        *,
        scope: str,
        key: str,
        limit: int,
        window_seconds: int = 3600,
    ) -> None:
        """Persist a timestamped attempt and reject over-budget callers.

        The budget lives in SQLite rather than process memory so restarting the
        service, or running several workers, cannot reset an attacker's window.
        A non-positive limit disables the check.
        """
        if limit <= 0:
            return
        now = time.time()
        cutoff = now - window_seconds
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM rate_limit_events WHERE at < ?", (cutoff,)
            )
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM rate_limit_events "
                "WHERE scope = ? AND key = ?",
                (scope, key),
            ).fetchone()
            if int(row["count"]) >= limit:
                connection.rollback()
                raise LicenseError(
                    "Too many attempts. Try again later.",
                    status_code=429,
                    code="rate_limited",
                )
            connection.execute(
                "INSERT INTO rate_limit_events (scope, key, at) VALUES (?, ?, ?)",
                (scope, key, now),
            )
            connection.commit()

    # ------------------------------------------------------------------ plans
    def plan_payloads(self, include_single_use: bool = False) -> list[dict[str, Any]]:
        plans = []
        for plan in sorted(
            _plans(), key=lambda item: item.monthly_price_kobo
        ):
            if plan.is_single_use and not include_single_use:
                continue
            plans.append(
                {
                    "id": plan.id,
                    "name": plan.name,
                    "tagline": plan.tagline,
                    "monthlyPriceKobo": plan.monthly_price_kobo,
                    "dailyHours": plan.daily_hours,
                    "maxDevices": plan.max_devices,
                    "maxWidth": plan.max_width,
                    "maxHeight": plan.max_height,
                    "maxFps": plan.max_fps,
                    "cloudMinutes": plan.cloud_minutes,
                    "features": list(plan.features),
                    "recommended": plan.recommended,
                    "isSingleUse": plan.is_single_use,
                    "terms": {
                        term: {
                            "label": spec["label"],
                            "months": spec["months"],
                            "discountPercent": spec["discount_percent"],
                            "priceKobo": plan.price_for(term),
                        }
                        for term, spec in TERMS.items()
                    },
                }
            )
        return plans


    # ----------------------------------------------------------------- orders
    def create_order(
        self, *, plan_id: str, term: str, email: str, provider: str
    ) -> tuple[dict[str, Any], str]:
        plan = get_plan(plan_id)
        if plan is None:
            raise LicenseError("Choose a valid plan.", code="unknown_plan")
        if term not in PLAN_TERMS:
            raise LicenseError("Choose a valid billing term.", code="unknown_term")
        email = email.strip()
        if len(email) > 254 or not EMAIL_RE.match(email):
            raise LicenseError("Enter a valid email address.", code="invalid_email")

        reference = f"ml_{uuid.uuid4().hex}"
        claim = new_claim_token()
        order = {
            "id": uuid.uuid4().hex,
            "reference": reference,
            "provider": provider,
            "email": email,
            "plan_id": plan.id,
            "term": term,
            "amount_kobo": plan.price_for(term),
            "currency": self._currency(provider),
            "status": "pending",
            "claim_hash": claim_hash(claim, self.settings.resolved_code_pepper),
            "created_at": iso(utcnow()),
            "paid_at": None,
        }
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO orders (
                    id, reference, provider, email, plan_id, term, amount_kobo,
                    currency, status, claim_hash, created_at, paid_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order["id"],
                    order["reference"],
                    order["provider"],
                    order["email"],
                    order["plan_id"],
                    order["term"],
                    order["amount_kobo"],
                    order["currency"],
                    order["status"],
                    order["claim_hash"],
                    order["created_at"],
                    order["paid_at"],
                ),
            )
            connection.commit()
        return order, claim

    def get_order(self, reference: str) -> dict[str, Any] | None:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM orders WHERE reference = ?", (reference,)
            ).fetchone()
        return _row(row) if row else None

    def get_order_by_claim(self, claim: str) -> dict[str, Any] | None:
        hashed = claim_hash(claim, self.settings.resolved_code_pepper)
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM orders WHERE claim_hash = ?", (hashed,)
            ).fetchone()
        return _row(row) if row else None

    def checkout_url(self, order: dict[str, Any], claim: str) -> str:
        try:
            provider = build_payment_provider(self.settings)
        except PaymentError as exc:
            raise LicenseError(str(exc), status_code=503, code="payment_unavailable")
        callback = (
            f"{self.settings.callback_base}/payment/callback?"
            f"provider={provider.name}&claim={claim}"
        )
        try:
            return provider.initialize(
                reference=order["reference"],
                email=order["email"],
                amount_kobo=order["amount_kobo"],
                currency=order["currency"],
                callback_url=callback,
            )
        except PaymentError as exc:
            raise LicenseError(str(exc), status_code=502, code="payment_error")

    def verify_and_fulfill(
        self, *, reference: str, provider_name: str
    ) -> dict[str, Any]:
        order = self.get_order(reference)
        if order is None:
            raise LicenseError("Order not found.", status_code=404, code="order_not_found")
        if order["status"] == "paid":
            return order
        if order["provider"] != provider_name:
            raise LicenseError("Payment provider mismatch.", code="provider_mismatch")
        try:
            provider = build_payment_provider(self.settings)
            verification = provider.verify(reference)
        except PaymentError as exc:
            raise LicenseError(str(exc), status_code=502, code="payment_error")
        return self.mark_paid(verification, provider_name=provider_name)

    def mark_paid(
        self, verification: PaymentVerification, *, provider_name: str
    ) -> dict[str, Any]:
        if not verification.success:
            raise LicenseError(
                "Payment was not successful.", status_code=402, code="payment_failed"
            )
        order = self.get_order(verification.reference)
        if order is None:
            raise LicenseError("Order not found.", status_code=404, code="order_not_found")
        if order["provider"] != provider_name:
            raise LicenseError("Payment provider mismatch.", code="provider_mismatch")
        if order["status"] == "paid":
            self._ensure_code(order)
            return order
        if verification.amount_kobo and verification.amount_kobo < order["amount_kobo"]:
            raise LicenseError(
                "The paid amount is less than the plan price.",
                status_code=402,
                code="amount_mismatch",
            )
        if verification.currency and not constant_time_equals(
            str(verification.currency).upper(), str(order["currency"]).upper()
        ):
            raise LicenseError(
                "The payment currency does not match the order.",
                status_code=402,
                code="currency_mismatch",
            )
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE orders SET status = 'paid', paid_at = ? WHERE id = ?",
                (iso(utcnow()), order["id"]),
            )
            connection.commit()
        order["status"] = "paid"
        self._ensure_code(order)
        self.send_receipt_if_needed(order)
        return order

    def send_receipt_if_needed(self, order: dict[str, Any]) -> bool:
        """Send receipt email to buyer via Resend once paid, idempotently."""
        if self.settings.is_mock_payments:
            return False
        if not self.settings.resend_api_key:
            return False
        if order.get("status") != "paid":
            return False

        with self.db.connect() as connection:
            cur = connection.execute(
                "UPDATE orders SET receipt_sent_at = ? WHERE id = ? AND receipt_sent_at IS NULL",
                (iso(utcnow()), order["id"]),
            )
            connection.commit()
            should_send = cur.rowcount > 0

        if not should_send:
            return False

        code = self.code_for_order(order)
        plan = get_plan(order["plan_id"])
        return send_receipt_email(
            order=order,
            plan=plan,
            code=code,
            settings=self.settings,
        )

    def record_webhook(
        self, *, event_id: str, provider: str, payload: bytes
    ) -> bool:
        """Record a webhook idempotently. Returns False for a duplicate."""
        digest = hashlib.sha256(payload).hexdigest()
        with self.db.connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO webhook_events (event_id, provider, received_at, payload_hash)
                    VALUES (?, ?, ?, ?)
                    """,
                    (event_id, provider, iso(utcnow()), digest),
                )
                connection.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    # ------------------------------------------------------------------ codes
    def code_for_order(self, order: dict[str, Any]) -> str:
        return derive_code(order["id"], self.settings.resolved_code_pepper)

    def _ensure_code(self, order: dict[str, Any]) -> dict[str, Any]:
        code = self.code_for_order(order)
        hashed = code_hash(code, self.settings.resolved_code_pepper)
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM subscription_codes WHERE order_id = ?", (order["id"],)
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO subscription_codes (
                        id, order_id, code_hash, plan_id, term, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'unused', ?)
                    """,
                    (
                        uuid.uuid4().hex,
                        order["id"],
                        hashed,
                        order["plan_id"],
                        order["term"],
                        iso(utcnow()),
                    ),
                )
                connection.commit()
                row = connection.execute(
                    "SELECT * FROM subscription_codes WHERE order_id = ?",
                    (order["id"],),
                ).fetchone()
        return _row(row) if row else {}

    # ------------------------------------------------------------- activation
    def activate(
        self, *, code: str, device_id: str, device_name: str = ""
    ) -> dict[str, Any]:
        try:
            normalized = normalize_code(code)
        except ValueError as exc:
            raise LicenseError(str(exc), code="invalid_code") from exc
        device_id = device_id.strip()
        if not device_id:
            raise LicenseError("A device id is required.", code="missing_device")
        hashed = code_hash(normalized, self.settings.resolved_code_pepper)

        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT c.*, o.status AS order_status
                    FROM subscription_codes c
                    JOIN orders o ON o.id = c.order_id
                    WHERE c.code_hash = ?
                    """,
                    (hashed,),
                ).fetchone()
                if row is None:
                    raise LicenseError(
                        "That code was not found. Check it and try again.",
                        code="unknown_code",
                    )
                code_row = _row(row)
                if code_row["order_status"] != "paid":
                    raise LicenseError(
                        "This code is not active yet. Contact support if payment succeeded.",
                        code="unpaid_code",
                    )
                if code_row["status"] == "revoked":
                    raise LicenseError(
                        "This code has been revoked.", code="revoked_code"
                    )
                plan = get_plan(code_row["plan_id"])
                if plan is None:
                    raise LicenseError(
                        "The plan for this code is no longer available.",
                        code="unknown_plan",
                    )

                # Re-entering the code on an already-activated device is a
                # harmless retry and returns that device's entitlement.
                existing = connection.execute(
                    "SELECT * FROM entitlements WHERE code_id = ? AND device_id = ?",
                    (code_row["id"], device_id),
                ).fetchone()
                if existing is not None:
                    entitlement = _row(existing)
                    if entitlement["status"] == "removed":
                        # "Remove this device" frees the slot but keeps the
                        # row, so the same machine can rejoin while a slot is
                        # free. A support revocation stays final.
                        now = utcnow()
                        if parse_iso(entitlement["expires_at"]) <= now:
                            raise LicenseError(
                                "This subscription has expired. Buy a new plan "
                                "and activate the new code.",
                                status_code=402,
                                code="code_expired",
                            )
                        active_count = connection.execute(
                            """
                            SELECT COUNT(*) AS count FROM entitlements
                            WHERE code_id = ? AND status = 'active'
                            """,
                            (code_row["id"],),
                        ).fetchone()
                        used = int(active_count["count"]) if active_count else 0
                        if used >= plan.max_devices:
                            raise LicenseError(
                                f"This subscription is already active on "
                                f"{used} of {plan.max_devices} allowed "
                                "device(s). Remove a device first, or contact "
                                "support to move it.",
                                status_code=409,
                                code="device_limit",
                            )
                        name = device_name.strip()[:120] or entitlement["device_name"]
                        connection.execute(
                            "UPDATE entitlements SET status = 'active', "
                            "device_name = ? WHERE id = ?",
                            (name, entitlement["id"]),
                        )
                        entitlement["status"] = "active"
                        entitlement["device_name"] = name
                        connection.commit()
                        return self._activation_response(entitlement)
                    if entitlement["status"] != "active":
                        raise LicenseError(
                            "This device was removed from the subscription by "
                            "support. Contact support to add it again.",
                            status_code=403,
                            code="revoked",
                        )
                    connection.commit()
                    return self._activation_response(entitlement)

                if code_row["status"] not in {"unused", "redeemed"}:
                    raise LicenseError(
                        "This code cannot be redeemed.", code="code_unusable"
                    )

                # Every device on one code shares the first activation's term,
                # even if it is added weeks later.
                first = connection.execute(
                    """
                    SELECT starts_at, expires_at FROM entitlements
                    WHERE code_id = ? ORDER BY created_at ASC LIMIT 1
                    """,
                    (code_row["id"],),
                ).fetchone()
                now = utcnow()
                if first is not None:
                    starts = parse_iso(first["starts_at"])
                    expires = parse_iso(first["expires_at"])
                elif code_row["redeemed_at"]:
                    starts = parse_iso(code_row["redeemed_at"])
                    expires = add_months(
                        starts, plan.months_for(code_row["term"])
                    )
                else:
                    starts = now
                    expires = add_months(
                        starts, plan.months_for(code_row["term"])
                    )
                if expires <= now:
                    raise LicenseError(
                        "This subscription has expired. Buy a new plan and "
                        "activate the new code.",
                        status_code=402,
                        code="code_expired",
                    )

                active_count = connection.execute(
                    """
                    SELECT COUNT(*) AS count FROM entitlements
                    WHERE code_id = ? AND status = 'active'
                    """,
                    (code_row["id"],),
                ).fetchone()
                used = int(active_count["count"]) if active_count else 0
                if used >= plan.max_devices:
                    raise LicenseError(
                        f"This subscription is already active on {used} of "
                        f"{plan.max_devices} allowed device(s). Remove a device "
                        "first, or contact support to move it.",
                        status_code=409,
                        code="device_limit",
                    )

                entitlement = {
                    "id": uuid.uuid4().hex,
                    "code_id": code_row["id"],
                    "order_id": code_row["order_id"],
                    "plan_id": plan.id,
                    "term": code_row["term"],
                    "device_id": device_id,
                    "device_name": device_name.strip()[:120],
                    "starts_at": iso(starts),
                    "expires_at": iso(expires),
                    "status": "active",
                    "created_at": iso(now),
                }
                connection.execute(
                    """
                    INSERT INTO entitlements (
                        id, code_id, order_id, plan_id, term, device_id,
                        device_name, starts_at, expires_at, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        entitlement["id"],
                        entitlement["code_id"],
                        entitlement["order_id"],
                        entitlement["plan_id"],
                        entitlement["term"],
                        entitlement["device_id"],
                        entitlement["device_name"],
                        entitlement["starts_at"],
                        entitlement["expires_at"],
                        entitlement["status"],
                        entitlement["created_at"],
                    ),
                )
                if code_row["status"] == "unused":
                    connection.execute(
                        "UPDATE subscription_codes SET status = 'redeemed', "
                        "redeemed_at = ?, redeemed_device = ? WHERE id = ?",
                        (iso(now), device_id, code_row["id"]),
                    )
                connection.commit()
                return self._activation_response(entitlement)
            except LicenseError:
                connection.rollback()
                raise
            except Exception:
                connection.rollback()
                raise

    def refresh(self, *, token: str, device_id: str) -> dict[str, Any]:
        payload = self._verify_token(token, device_id)
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM entitlements WHERE id = ?", (payload["sub"],)
            ).fetchone()
        if row is None:
            raise LicenseError(
                "This subscription no longer exists.", status_code=404, code="not_found"
            )
        entitlement = _row(row)
        if entitlement["status"] != "active":
            raise LicenseError(
                "This subscription has been revoked.", status_code=403, code="revoked"
            )
        return self._activation_response(entitlement)

    def deactivate(self, *, token: str, device_id: str) -> dict[str, Any]:
        """Remove this device from the subscription and free its slot.

        The row is kept with status `removed` so the same machine can activate
        the same code again while a slot is free. Support revocations use
        `revoked` and cannot be undone by the client.
        """
        payload = self._verify_token(token, device_id)
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM entitlements WHERE id = ? AND device_id = ?",
                (payload["sub"], device_id),
            ).fetchone()
            if row is None:
                raise LicenseError(
                    "This device's activation was not found.",
                    status_code=404,
                    code="not_found",
                )
            entitlement = _row(row)
            if entitlement["status"] == "revoked":
                # A support revocation is final. Without this guard the client
                # could call deactivate and turn `revoked` into `removed`,
                # then rejoin the code with the same signed token.
                raise LicenseError(
                    "This subscription was revoked by support.",
                    status_code=403,
                    code="revoked",
                )
            if entitlement["status"] == "removed":
                return {"ok": True, "status": "removed"}
            connection.execute(
                "UPDATE entitlements SET status = 'removed' "
                "WHERE id = ? AND status = 'active'",
                (payload["sub"],),
            )
            connection.commit()
        return {"ok": True, "status": "removed"}


    # ------------------------------------------------------------------ usage
    def record_usage(
        self,
        *,
        token: str,
        device_id: str,
        event_id: str,
        seconds: int,
    ) -> dict[str, Any]:
        payload = self._verify_token(token, device_id)
        seconds = max(0, min(int(seconds), self.settings.max_usage_delta_seconds))
        event_id = event_id.strip()[:96]
        if not event_id:
            raise LicenseError("A usage event id is required.", code="missing_event")
        now = utcnow()
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM entitlements WHERE id = ?", (payload["sub"],)
            ).fetchone()
            if row is None:
                raise LicenseError(
                    "This subscription no longer exists.",
                    status_code=404,
                    code="not_found",
                )
            entitlement = _row(row)
            if entitlement["status"] != "active":
                raise LicenseError(
                    "This subscription has been revoked.",
                    status_code=403,
                    code="revoked",
                )
            if parse_iso(entitlement["expires_at"]) <= now:
                raise LicenseError(
                    "This subscription has expired.",
                    status_code=402,
                    code="expired",
                )

            # The client reports its own elapsed time, but a modified client
            # could report zero forever. The server also measures the gap
            # between this device's heartbeats and charges the larger value,
            # capped like a normal delta. Long gaps (sleep, offline) fall back
            # to the reported value so a resumed app is not billed for hours.
            elapsed = 0
            if entitlement.get("last_usage_at"):
                try:
                    elapsed = int(
                        (now - parse_iso(entitlement["last_usage_at"]))
                        .total_seconds()
                    )
                except ValueError:
                    elapsed = 0
                if (
                    elapsed < 0
                    or elapsed > self.settings.max_usage_delta_seconds * 2
                ):
                    elapsed = 0
            charge = min(
                max(seconds, elapsed), self.settings.max_usage_delta_seconds
            )

            day = now.date().isoformat()
            plan = get_plan(entitlement["plan_id"])
            if plan is None:
                raise LicenseError("Unknown plan.", code="unknown_plan")
            existing = connection.execute(
                "SELECT 1 FROM usage_events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if existing is None and charge > 0:
                connection.execute(
                    """
                    INSERT INTO usage_events (
                        event_id, entitlement_id, code_id, day, seconds, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        entitlement["id"],
                        entitlement["code_id"],
                        day,
                        charge,
                        iso(now),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO usage_daily (code_id, day, seconds, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(code_id, day) DO UPDATE SET
                        seconds = seconds + excluded.seconds,
                        updated_at = excluded.updated_at
                    """,
                    (entitlement["code_id"], day, charge, iso(now)),
                )
            connection.execute(
                "UPDATE entitlements SET last_usage_at = ? WHERE id = ?",
                (iso(now), entitlement["id"]),
            )
            connection.commit()
            return self._usage_payload(connection, entitlement, plan, day)

    def usage_for(self, entitlement: dict[str, Any]) -> dict[str, Any]:
        plan = get_plan(entitlement["plan_id"])
        if plan is None:
            raise LicenseError("Unknown plan.", code="unknown_plan")
        with self.db.connect() as connection:
            return self._usage_payload(
                connection,
                entitlement,
                plan,
                utcnow().date().isoformat(),
            )

    # --------------------------------------------------------------- helpers
    def _verify_token(self, token: str, device_id: str) -> dict[str, Any]:
        try:
            payload = verify_entitlement(token, self.settings.license_secret)
        except ValueError as exc:
            raise LicenseError(
                str(exc), status_code=401, code="invalid_token"
            ) from exc
        if payload.get("device") != device_id:
            raise LicenseError(
                "This subscription belongs to a different device.",
                status_code=403,
                code="device_mismatch",
            )
        return payload

    def _activation_response(self, entitlement: dict[str, Any]) -> dict[str, Any]:
        plan = get_plan(entitlement["plan_id"])
        if plan is None:
            raise LicenseError("Unknown plan.", code="unknown_plan")
        expires = parse_iso(entitlement["expires_at"])
        token = sign_entitlement(
            {
                "v": 1,
                "sub": entitlement["id"],
                "code": entitlement["code_id"],
                "plan": entitlement["plan_id"],
                "term": entitlement["term"],
                "device": entitlement["device_id"],
                "iat": int(parse_iso(entitlement["starts_at"]).timestamp()),
                "exp": int(expires.timestamp()),
            },
            self.settings.license_secret,
        )
        with self.db.connect() as connection:
            usage = self._usage_payload(
                connection,
                entitlement,
                plan,
                utcnow().date().isoformat(),
            )
            device_row = connection.execute(
                """
                SELECT COUNT(*) AS count FROM entitlements
                WHERE code_id = ? AND status = 'active'
                """,
                (entitlement["code_id"],),
            ).fetchone()
            devices_used = int(device_row["count"]) if device_row else 1
        return {
            "token": token,
            "entitlement": {
                "id": entitlement["id"],
                "planId": plan.id,
                "planName": plan.name,
                "term": entitlement["term"],
                "startsAt": entitlement["starts_at"],
                "expiresAt": entitlement["expires_at"],
                "dailyHours": plan.daily_hours,
                "maxDevices": plan.max_devices,
                "devicesUsed": devices_used,
                "maxWidth": plan.max_width,
                "maxHeight": plan.max_height,
                "maxFps": plan.max_fps,
                "cloudMinutes": plan.cloud_minutes,
                "status": entitlement["status"],
            },
            "usage": usage,
        }

    def _usage_payload(
        self,
        connection: sqlite3.Connection,
        entitlement: dict[str, Any],
        plan: Plan,
        day: str,
    ) -> dict[str, Any]:
        if plan.is_single_use:
            # Lifetime total across every day — once exhausted, it's gone forever.
            total_row = connection.execute(
                "SELECT COALESCE(SUM(seconds), 0) AS total FROM usage_daily"
                " WHERE code_id = ?",
                (entitlement["code_id"],),
            ).fetchone()
            used = int(total_row["total"]) if total_row else 0
            limit = plan.daily_hours * 3600
            return {
                "day": day,
                "usedSeconds": used,
                "limitSeconds": limit,
                "remainingSeconds": max(0, limit - used),
            }
        row = connection.execute(
            "SELECT seconds FROM usage_daily WHERE code_id = ? AND day = ?",
            (entitlement["code_id"], day),
        ).fetchone()
        used = int(row["seconds"]) if row else 0
        limit = plan.daily_hours * 3600
        return {
            "day": day,
            "usedSeconds": used,
            "limitSeconds": limit,
            "remainingSeconds": max(0, limit - used),
        }


    def _currency(self, provider: str) -> str:
        if provider == "paystack":
            return self.settings.paystack_currency
        if provider == "flutterwave":
            return self.settings.flutterwave_currency
        return "NGN"


def _plans() -> list[Plan]:
    try:
        from .plans import PLANS
    except (ImportError, ValueError):
        from plans import PLANS

    return list(PLANS.values())


def _row(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}
