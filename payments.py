"""Hosted-checkout adapters for Paystack, Flutterwave, and local testing.

The browser redirect is never trusted on its own. Fulfilment always verifies
the transaction with the provider API or a signed webhook before a code is
created.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any, Mapping, Protocol
from urllib.parse import urlencode

import requests

try:
    from .config import Settings
    from .security import constant_time_equals
except (ImportError, ValueError):
    from config import Settings
    from security import constant_time_equals


class PaymentError(RuntimeError):
    pass


@dataclass(frozen=True)
class PaymentVerification:
    success: bool
    reference: str
    amount_kobo: int
    currency: str
    event_id: str
    raw: dict[str, Any]


class PaymentProvider(Protocol):
    name: str

    def initialize(
        self,
        *,
        reference: str,
        email: str,
        amount_kobo: int,
        currency: str,
        callback_url: str,
    ) -> str:
        """Return the hosted checkout URL."""

    def verify(self, reference: str) -> PaymentVerification:
        """Ask the provider whether a transaction succeeded."""

    def parse_webhook(
        self, headers: Mapping[str, str], body: bytes
    ) -> PaymentVerification | None:
        """Validate a webhook and return its payment, or None if irrelevant."""


class MockPaymentProvider:
    """Local-only provider. The checkout page exposes a test-payment button."""

    name = "mock"

    def initialize(
        self,
        *,
        reference: str,
        email: str,
        amount_kobo: int,
        currency: str,
        callback_url: str,
    ) -> str:
        query = urlencode({"reference": reference, "claim": _claim_from_url(callback_url)})
        return f"/mock/pay?{query}"

    def verify(self, reference: str) -> PaymentVerification:
        return PaymentVerification(
            success=True,
            reference=reference,
            amount_kobo=0,
            currency="NGN",
            event_id=f"mock:{reference}",
            raw={"provider": "mock"},
        )

    def parse_webhook(
        self, headers: Mapping[str, str], body: bytes
    ) -> PaymentVerification | None:
        return None


class PaystackPaymentProvider:
    name = "paystack"
    api = "https://api.paystack.co"

    def __init__(self, secret_key: str, currency: str = "NGN"):
        self.secret_key = secret_key
        self.currency = currency

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.secret_key}",
            "Content-Type": "application/json",
        }

    def initialize(
        self,
        *,
        reference: str,
        email: str,
        amount_kobo: int,
        currency: str,
        callback_url: str,
    ) -> str:
        payload = {
            "email": email,
            "amount": int(amount_kobo),
            "currency": currency,
            "reference": reference,
            "callback_url": callback_url,
            "metadata": {"product": "moonlight-studio"},
        }
        try:
            response = requests.post(
                f"{self.api}/transaction/initialize",
                json=payload,
                headers=self._headers,
                timeout=20,
            )
        except requests.RequestException as exc:
            raise PaymentError("Could not reach Paystack. Try again.") from exc
        data = _json(response)
        url = (data.get("data") or {}).get("authorization_url")
        if not response.ok or not data.get("status") or not url:
            message = data.get("message") or "Paystack rejected the checkout."
            raise PaymentError(str(message))
        return str(url)

    def verify(self, reference: str) -> PaymentVerification:
        try:
            response = requests.get(
                f"{self.api}/transaction/verify/{reference}",
                headers=self._headers,
                timeout=20,
            )
        except requests.RequestException as exc:
            raise PaymentError("Could not verify the Paystack payment.") from exc
        body = _json(response)
        data = body.get("data") or {}
        success = bool(body.get("status")) and data.get("status") == "success"
        return PaymentVerification(
            success=success,
            reference=str(data.get("reference") or reference),
            amount_kobo=int(data.get("amount") or 0),
            currency=str(data.get("currency") or self.currency),
            event_id=str(data.get("id") or reference),
            raw=body,
        )

    def parse_webhook(
        self, headers: Mapping[str, str], body: bytes
    ) -> PaymentVerification | None:
        supplied = _header(headers, "x-paystack-signature")
        expected = hmac.new(
            self.secret_key.encode("utf-8"), body, hashlib.sha512
        ).hexdigest()
        if not supplied or not constant_time_equals(expected, supplied):
            raise PaymentError("Invalid Paystack webhook signature.")
        try:
            event = json.loads(body)
        except json.JSONDecodeError as exc:
            raise PaymentError("Malformed Paystack webhook.") from exc
        if event.get("event") != "charge.success":
            return None
        data = event.get("data") or {}
        return PaymentVerification(
            success=data.get("status") == "success",
            reference=str(data.get("reference") or ""),
            amount_kobo=int(data.get("amount") or 0),
            currency=str(data.get("currency") or self.currency),
            event_id=str(data.get("id") or data.get("reference") or ""),
            raw=event,
        )


class FlutterwavePaymentProvider:
    name = "flutterwave"
    api = "https://api.flutterwave.com/v3"

    def __init__(self, secret_key: str, secret_hash: str, currency: str = "NGN"):
        self.secret_key = secret_key
        self.secret_hash = secret_hash
        self.currency = currency

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.secret_key}",
            "Content-Type": "application/json",
        }

    def initialize(
        self,
        *,
        reference: str,
        email: str,
        amount_kobo: int,
        currency: str,
        callback_url: str,
    ) -> str:
        payload = {
            "tx_ref": reference,
            "amount": round(amount_kobo / 100, 2),
            "currency": currency,
            "redirect_url": callback_url,
            "customer": {"email": email},
            "customizations": {
                "title": "Moonlight Studio",
                "description": "Moonlight Studio subscription",
            },
        }
        try:
            response = requests.post(
                f"{self.api}/payments",
                json=payload,
                headers=self._headers,
                timeout=20,
            )
        except requests.RequestException as exc:
            raise PaymentError("Could not reach Flutterwave. Try again.") from exc
        body = _json(response)
        link = (body.get("data") or {}).get("link")
        if not response.ok or body.get("status") != "success" or not link:
            message = body.get("message") or "Flutterwave rejected the checkout."
            raise PaymentError(str(message))
        return str(link)

    def verify(self, reference: str) -> PaymentVerification:
        try:
            response = requests.get(
                f"{self.api}/transactions/verify_by_reference",
                params={"tx_ref": reference},
                headers=self._headers,
                timeout=20,
            )
        except requests.RequestException as exc:
            raise PaymentError("Could not verify the Flutterwave payment.") from exc
        body = _json(response)
        data = body.get("data") or {}
        return PaymentVerification(
            success=body.get("status") == "success"
            and data.get("status") == "successful",
            reference=str(data.get("tx_ref") or reference),
            amount_kobo=int(round(float(data.get("amount") or 0) * 100)),
            currency=str(data.get("currency") or self.currency),
            event_id=str(data.get("id") or reference),
            raw=body,
        )

    def parse_webhook(
        self, headers: Mapping[str, str], body: bytes
    ) -> PaymentVerification | None:
        supplied = _header(headers, "verif-hash")
        if not supplied or not constant_time_equals(self.secret_hash, supplied):
            raise PaymentError("Invalid Flutterwave webhook signature.")
        try:
            event = json.loads(body)
        except json.JSONDecodeError as exc:
            raise PaymentError("Malformed Flutterwave webhook.") from exc
        data = event.get("data") or {}
        return PaymentVerification(
            success=data.get("status") == "successful",
            reference=str(data.get("tx_ref") or ""),
            amount_kobo=int(round(float(data.get("amount") or 0) * 100)),
            currency=str(data.get("currency") or self.currency),
            event_id=str(data.get("id") or data.get("tx_ref") or ""),
            raw=event,
        )


def build_payment_provider(settings: Settings) -> PaymentProvider:
    ready, reason = settings.payment_ready()
    if not ready:
        raise PaymentError(reason)
    provider = settings.payment_provider.lower()
    if provider == "mock":
        return MockPaymentProvider()
    if provider == "paystack":
        return PaystackPaymentProvider(
            settings.paystack_secret_key, settings.paystack_currency
        )
    if provider == "flutterwave":
        return FlutterwavePaymentProvider(
            settings.flutterwave_secret_key,
            settings.flutterwave_secret_hash,
            settings.flutterwave_currency,
        )
    raise PaymentError(f"Unknown payment provider '{provider}'.")


def _claim_from_url(url: str) -> str:
    from urllib.parse import parse_qs, urlparse

    return parse_qs(urlparse(url).query).get("claim", [""])[0]


def _header(headers: Mapping[str, str], name: str) -> str:
    for key, value in headers.items():
        if key.lower() == name:
            return str(value)
    return ""


def _json(response: requests.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError as exc:
        raise PaymentError(
            f"Payment provider returned an unreadable response ({response.status_code})."
        ) from exc
    if not isinstance(data, dict):
        raise PaymentError("Payment provider returned an unexpected response.")
    return data
