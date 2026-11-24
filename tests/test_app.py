from __future__ import annotations

import hashlib
import hmac
import json
import re
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

try:
    from server.app import create_app
    from server.config import Settings
except ModuleNotFoundError:
    from app import create_app
    from config import Settings

CODE_RE = re.compile(r"ML-[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}")


class AppTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings(
            database_path=Path(self.temp.name) / "web.sqlite3",
            license_secret="test-license-secret",
            code_pepper="test-code-pepper",
            payment_provider="mock",
            public_base_url="http://testserver",
        )
        self.app = create_app(self.settings)
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_public_pages_render(self) -> None:
        for path in ("/", "/pricing", "/activate", "/healthz"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
        self.assertIn("Moonlight Studio", self.client.get("/").text)
        self.assertIn("Starter", self.client.get("/pricing").text)

    def test_mock_checkout_to_activation(self) -> None:
        response = self.client.post(
            "/checkout",
            data={
                "plan_id": "starter",
                "term": "monthly",
                "email": "buyer@example.com",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        location = response.headers["location"]
        self.assertTrue(location.startswith("/mock/pay?"))

        pay_page = self.client.get(location)
        self.assertEqual(pay_page.status_code, 200)
        reference = re.search(
            r'name="reference" value="([^"]+)"', pay_page.text
        ).group(1)
        claim = re.search(r'name="claim" value="([^"]+)"', pay_page.text).group(1)

        paid = self.client.post(
            "/mock/pay",
            data={"reference": reference, "claim": claim},
            follow_redirects=False,
        )
        self.assertEqual(paid.status_code, 303)

        success = self.client.get(paid.headers["location"])
        self.assertEqual(success.status_code, 200)
        match = CODE_RE.search(success.text)
        self.assertIsNotNone(match)
        code = match.group(0)

        activation = self.client.post(
            "/api/v1/activate",
            json={
                "code": code,
                "deviceId": "device-a",
                "deviceName": "Test PC",
            },
        )
        self.assertEqual(activation.status_code, 200)
        body = activation.json()
        self.assertEqual(body["entitlement"]["planId"], "starter")
        self.assertEqual(body["usage"]["remainingSeconds"], 7200)

        usage = self.client.post(
            "/api/v1/usage",
            json={
                "token": body["token"],
                "deviceId": "device-a",
                "eventId": "event-1",
                "seconds": 120,
            },
        )
        self.assertEqual(usage.status_code, 200)
        self.assertEqual(usage.json()["remainingSeconds"], 7080)

    def test_rejects_bad_email_and_unknown_plan(self) -> None:
        bad_email = self.client.post(
            "/checkout",
            data={"plan_id": "starter", "term": "monthly", "email": "nope"},
        )
        self.assertEqual(bad_email.status_code, 400)
        unknown = self.client.post(
            "/checkout",
            data={
                "plan_id": "enterprise",
                "term": "monthly",
                "email": "buyer@example.com",
            },
        )
        self.assertEqual(unknown.status_code, 400)

    def test_activation_rejects_unknown_code(self) -> None:
        response = self.client.post(
            "/api/v1/activate",
            json={"code": "ML-AAAA-BBBB-CCCC", "deviceId": "device-a"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "unknown_code")

    def test_paystack_webhook_fulfils_once(self) -> None:
        settings = Settings(
            database_path=Path(self.temp.name) / "paystack.sqlite3",
            license_secret="test-license-secret",
            code_pepper="test-code-pepper",
            payment_provider="paystack",
            paystack_secret_key="sk_test_moonlight",
            public_base_url="http://testserver",
        )
        app = create_app(settings)
        client = TestClient(app)
        order, _ = app.state.service.create_order(
            plan_id="creator",
            term="six_months",
            email="buyer@example.com",
            provider="paystack",
        )
        payload = {
            "event": "charge.success",
            "data": {
                "id": 991,
                "reference": order["reference"],
                "status": "success",
                "amount": order["amount_kobo"],
                "currency": "NGN",
            },
        }
        body = json.dumps(payload, separators=(",", ":")).encode()
        signature = hmac.new(
            b"sk_test_moonlight", body, hashlib.sha512
        ).hexdigest()
        headers = {
            "content-type": "application/json",
            "x-paystack-signature": signature,
        }
        first = client.post("/webhooks/paystack", content=body, headers=headers)
        self.assertEqual(first.status_code, 200, first.text)
        paid = app.state.service.get_order(order["reference"])
        self.assertEqual(paid["status"], "paid")

        second = client.post("/webhooks/paystack", content=body, headers=headers)
        self.assertEqual(second.status_code, 200, second.text)
        code = app.state.service.code_for_order(paid)
        self.assertRegex(code, CODE_RE)

    def test_paystack_webhook_rejects_bad_signature(self) -> None:
        settings = Settings(
            database_path=Path(self.temp.name) / "paystack-bad.sqlite3",
            license_secret="test-license-secret",
            code_pepper="test-code-pepper",
            payment_provider="paystack",
            paystack_secret_key="sk_test_moonlight",
            public_base_url="http://testserver",
        )
        client = TestClient(create_app(settings))
        response = client.post(
            "/webhooks/paystack",
            content=b'{"event":"charge.success"}',
            headers={
                "content-type": "application/json",
                "x-paystack-signature": "bad",
            },
        )
        self.assertEqual(response.status_code, 502)

    def test_mock_pay_requires_a_matching_claim(self) -> None:
        order, claim = self.app.state.service.create_order(
            plan_id="starter",
            term="monthly",
            email="buyer@example.com",
            provider="mock",
        )
        good = self.client.get(
            f"/mock/pay?reference={order['reference']}&claim={claim}"
        )
        self.assertEqual(good.status_code, 200)

        wrong = self.client.get(
            f"/mock/pay?reference={order['reference']}&claim=not-the-claim"
        )
        self.assertEqual(wrong.status_code, 404)

        posted = self.client.post(
            "/mock/pay",
            data={"reference": order["reference"], "claim": "not-the-claim"},
            follow_redirects=False,
        )
        self.assertEqual(posted.status_code, 404)
        self.assertEqual(
            self.app.state.service.get_order(order["reference"])["status"],
            "pending",
        )

    def test_activation_rate_limit_returns_429(self) -> None:
        settings = Settings(
            database_path=Path(self.temp.name) / "limited.sqlite3",
            license_secret="test-license-secret",
            code_pepper="test-code-pepper",
            payment_provider="mock",
            public_base_url="http://testserver",
            activation_attempts_per_hour=2,
        )
        client = TestClient(create_app(settings))
        payload = {"code": "ML-AAAA-BBBB-CCCC", "deviceId": "device-a"}
        self.assertEqual(client.post("/api/v1/activate", json=payload).status_code, 400)
        self.assertEqual(client.post("/api/v1/activate", json=payload).status_code, 400)
        blocked = client.post("/api/v1/activate", json=payload)
        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(blocked.json()["code"], "rate_limited")

    def test_production_refuses_mock_payments(self) -> None:
        settings = Settings(
            database_path=Path(self.temp.name) / "prod.sqlite3",
            license_secret="a-real-production-secret",
            code_pepper="a-real-pepper",
            payment_provider="mock",
            public_base_url="https://studio.example.com",
        )
        with self.assertRaises(RuntimeError):
            create_app(settings)

    def test_production_refuses_default_secret(self) -> None:
        settings = Settings(
            database_path=Path(self.temp.name) / "prod-secret.sqlite3",
            payment_provider="paystack",
            paystack_secret_key="sk_test",
            public_base_url="https://studio.example.com",
        )
        with self.assertRaises(RuntimeError):
            create_app(settings)


if __name__ == "__main__":
    unittest.main()
