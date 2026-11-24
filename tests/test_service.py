"""Tests for the license service. Run from the repository root:

    .\.venv\Scripts\python.exe -m unittest discover -s server/tests
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

try:
    from server.config import Settings
    from server.db import Database
    from server.payments import PaymentVerification
    from server.plans import add_months
    from server.security import iso, utcnow
    from server.service import LicenseError, LicenseService
except ModuleNotFoundError:
    from config import Settings
    from db import Database
    from payments import PaymentVerification
    from plans import add_months
    from security import iso, utcnow
    from service import LicenseError, LicenseService


class ServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings(
            database_path=Path(self.temp.name) / "test.sqlite3",
            license_secret="test-license-secret",
            code_pepper="test-code-pepper",
            payment_provider="mock",
            public_base_url="http://testserver",
            max_usage_delta_seconds=3600,
        )
        self.database = Database(self.settings.database_path)
        self.database.initialize()
        self.service = LicenseService(self.database, self.settings)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _paid_order(self, plan_id: str = "starter", term: str = "monthly"):
        order, claim = self.service.create_order(
            plan_id=plan_id,
            term=term,
            email="buyer@example.com",
            provider="mock",
        )
        self.service.mark_paid(
            PaymentVerification(
                success=True,
                reference=order["reference"],
                amount_kobo=0,
                currency=order["currency"],
                event_id="evt-1",
                raw={},
            ),
            provider_name="mock",
        )
        return self.service.get_order(order["reference"]), claim

    def test_purchase_activation_and_same_device_retry(self) -> None:
        order, _ = self._paid_order()
        code = self.service.code_for_order(order)
        first = self.service.activate(
            code=code, device_id="device-a", device_name="Desktop"
        )
        self.assertEqual(first["entitlement"]["planId"], "starter")
        self.assertEqual(first["entitlement"]["dailyHours"], 2)
        self.assertTrue(first["token"])

        again = self.service.activate(
            code=code, device_id="device-a", device_name="Desktop"
        )
        self.assertEqual(again["entitlement"]["id"], first["entitlement"]["id"])

        with self.assertRaises(LicenseError) as raised:
            self.service.activate(code=code, device_id="device-b")
        self.assertEqual(raised.exception.code, "device_limit")

    def test_creator_code_activates_two_devices_with_shared_usage(self) -> None:
        order, _ = self._paid_order(plan_id="creator", term="monthly")
        code = self.service.code_for_order(order)
        first = self.service.activate(code=code, device_id="device-a")
        second = self.service.activate(code=code, device_id="device-b")
        self.assertEqual(first["entitlement"]["expiresAt"], second["entitlement"]["expiresAt"])
        self.assertEqual(second["entitlement"]["devicesUsed"], 2)

        with self.assertRaises(LicenseError) as raised:
            self.service.activate(code=code, device_id="device-c")
        self.assertEqual(raised.exception.code, "device_limit")

        # The daily pool is shared by the subscription, not per device.
        used_a = self.service.record_usage(
            token=first["token"],
            device_id="device-a",
            event_id="a-1",
            seconds=3600,
        )
        self.assertEqual(used_a["usedSeconds"], 3600)
        used_b = self.service.record_usage(
            token=second["token"],
            device_id="device-b",
            event_id="b-1",
            seconds=60,
        )
        self.assertEqual(used_b["usedSeconds"], 3660)
        self.assertEqual(used_b["remainingSeconds"], 4 * 3600 - 3660)

    def test_deactivating_a_device_frees_its_slot(self) -> None:
        order, _ = self._paid_order(plan_id="studio", term="monthly")
        code = self.service.code_for_order(order)
        tokens = {}
        for name in ("device-a", "device-b", "device-c"):
            tokens[name] = self.service.activate(code=code, device_id=name)["token"]
        with self.assertRaises(LicenseError) as raised:
            self.service.activate(code=code, device_id="device-d")
        self.assertEqual(raised.exception.code, "device_limit")

        removed = self.service.deactivate(
            token=tokens["device-b"], device_id="device-b"
        )
        self.assertEqual(removed["status"], "removed")
        replacement = self.service.activate(code=code, device_id="device-d")
        self.assertEqual(replacement["entitlement"]["devicesUsed"], 3)
        with self.assertRaises(LicenseError) as revoked:
            self.service.refresh(token=tokens["device-b"], device_id="device-b")
        self.assertEqual(revoked.exception.code, "revoked")

    def test_removed_device_can_rejoin_while_a_slot_is_free(self) -> None:
        order, _ = self._paid_order(plan_id="creator", term="monthly")
        code = self.service.code_for_order(order)
        first = self.service.activate(code=code, device_id="device-a")
        self.service.activate(code=code, device_id="device-b")

        self.service.deactivate(token=first["token"], device_id="device-a")
        rejoined = self.service.activate(code=code, device_id="device-a")
        self.assertEqual(rejoined["entitlement"]["devicesUsed"], 2)
        refreshed = self.service.refresh(
            token=rejoined["token"], device_id="device-a"
        )
        self.assertEqual(refreshed["entitlement"]["status"], "active")

        # Once both slots are taken by other machines, the removed device
        # must free one again before it can rejoin.
        self.service.deactivate(token=rejoined["token"], device_id="device-a")
        self.service.activate(code=code, device_id="device-c")
        with self.assertRaises(LicenseError) as raised:
            self.service.activate(code=code, device_id="device-a")
        self.assertEqual(raised.exception.code, "device_limit")

    def test_invalid_code_is_rejected(self) -> None:
        with self.assertRaises(LicenseError) as raised:
            self.service.activate(code="ML-AAAA-BBBB-CCCC", device_id="device-a")
        self.assertEqual(raised.exception.code, "unknown_code")

    def test_daily_usage_is_capped_and_idempotent(self) -> None:
        order, _ = self._paid_order()
        activated = self.service.activate(
            code=self.service.code_for_order(order), device_id="device-a"
        )
        token = activated["token"]

        first = self.service.record_usage(
            token=token, device_id="device-a", event_id="e1", seconds=3600
        )
        self.assertEqual(first["usedSeconds"], 3600)
        self.assertEqual(first["remainingSeconds"], 3600)

        duplicate = self.service.record_usage(
            token=token, device_id="device-a", event_id="e1", seconds=3600
        )
        self.assertEqual(duplicate["usedSeconds"], 3600)

        second = self.service.record_usage(
            token=token, device_id="device-a", event_id="e2", seconds=3600
        )
        self.assertEqual(second["remainingSeconds"], 0)

        overflow = self.service.record_usage(
            token=token, device_id="device-a", event_id="e3", seconds=60
        )
        self.assertEqual(overflow["usedSeconds"], 7260)
        self.assertEqual(overflow["remainingSeconds"], 0)

    def test_usage_rejects_a_different_device(self) -> None:
        order, _ = self._paid_order()
        activated = self.service.activate(
            code=self.service.code_for_order(order), device_id="device-a"
        )
        with self.assertRaises(LicenseError) as raised:
            self.service.record_usage(
                token=activated["token"],
                device_id="device-b",
                event_id="e1",
                seconds=60,
            )
        self.assertEqual(raised.exception.code, "device_mismatch")

    def test_expired_entitlement_rejects_usage(self) -> None:
        order, _ = self._paid_order()
        activated = self.service.activate(
            code=self.service.code_for_order(order), device_id="device-a"
        )
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE entitlements SET expires_at = ? WHERE id = ?",
                (
                    iso(utcnow() - timedelta(minutes=1)),
                    activated["entitlement"]["id"],
                ),
            )
            connection.commit()
        with self.assertRaises(LicenseError) as raised:
            self.service.record_usage(
                token=activated["token"],
                device_id="device-a",
                event_id="e1",
                seconds=60,
            )
        self.assertEqual(raised.exception.code, "expired")

    def test_revoked_entitlement_cannot_refresh(self) -> None:
        order, _ = self._paid_order()
        activated = self.service.activate(
            code=self.service.code_for_order(order), device_id="device-a"
        )
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE entitlements SET status = 'revoked' WHERE id = ?",
                (activated["entitlement"]["id"],),
            )
            connection.commit()
        with self.assertRaises(LicenseError) as raised:
            self.service.refresh(
                token=activated["token"], device_id="device-a"
            )
        self.assertEqual(raised.exception.code, "revoked")

    def test_underpayment_is_rejected(self) -> None:
        order, _ = self.service.create_order(
            plan_id="creator",
            term="monthly",
            email="buyer@example.com",
            provider="mock",
        )
        with self.assertRaises(LicenseError) as raised:
            self.service.mark_paid(
                PaymentVerification(
                    success=True,
                    reference=order["reference"],
                    amount_kobo=1,
                    currency="NGN",
                    event_id="evt",
                    raw={},
                ),
                provider_name="mock",
            )
        self.assertEqual(raised.exception.code, "amount_mismatch")

    def test_longer_terms_are_discounted(self) -> None:
        service_plans = {plan["id"]: plan for plan in self.service.plan_payloads()}
        for plan in service_plans.values():
            self.assertGreaterEqual(plan["monthlyPriceKobo"], 150_000 * 100)
        creator = service_plans["creator"]
        monthly = creator["terms"]["monthly"]["priceKobo"]
        six = creator["terms"]["six_months"]["priceKobo"]
        yearly = creator["terms"]["yearly"]["priceKobo"]
        self.assertLess(six, monthly * 6)
        self.assertLess(yearly, monthly * 12)
        self.assertEqual(six, ((monthly * 6 * 85 + 5_000) // 10_000) * 100)
        self.assertEqual(yearly, ((monthly * 12 * 75 + 5_000) // 10_000) * 100)
        self.assertEqual(monthly, 300_000 * 100)

    def test_add_months_clamps_end_of_month(self) -> None:
        start = utcnow().replace(
            year=2026, month=1, day=31, hour=10, minute=0, second=0, microsecond=0
        )
        self.assertEqual(add_months(start, 1).day, 28)
        self.assertEqual(add_months(start, 12).day, 31)

    def test_support_revocation_is_not_undoable_by_deactivate(self) -> None:
        order, _ = self._paid_order()
        code = self.service.code_for_order(order)
        activated = self.service.activate(code=code, device_id="device-a")
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE entitlements SET status = 'revoked' WHERE id = ?",
                (activated["entitlement"]["id"],),
            )
            connection.commit()

        with self.assertRaises(LicenseError) as raised:
            self.service.deactivate(
                token=activated["token"], device_id="device-a"
            )
        self.assertEqual(raised.exception.code, "revoked")

        # The slot must stay blocked; a revoked row cannot be turned into a
        # removable row and then rejoined with the same token.
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT status FROM entitlements WHERE id = ?",
                (activated["entitlement"]["id"],),
            ).fetchone()
        self.assertEqual(row["status"], "revoked")
        with self.assertRaises(LicenseError) as rejoined:
            self.service.activate(code=code, device_id="device-a")
        self.assertEqual(rejoined.exception.code, "revoked")

    def test_server_heartbeat_charges_a_zero_report(self) -> None:
        order, _ = self._paid_order()
        activated = self.service.activate(
            code=self.service.code_for_order(order), device_id="device-a"
        )
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE entitlements SET last_usage_at = ? WHERE id = ?",
                (
                    iso(utcnow() - timedelta(seconds=90)),
                    activated["entitlement"]["id"],
                ),
            )
            connection.commit()

        # A modified client that under-reports still gets charged for the
        # wall-clock gap between its heartbeats.
        usage = self.service.record_usage(
            token=activated["token"],
            device_id="device-a",
            event_id="under-reported",
            seconds=0,
        )
        self.assertGreaterEqual(usage["usedSeconds"], 89)
        self.assertLessEqual(usage["usedSeconds"], 92)

    def test_rate_limit_is_persistent(self) -> None:
        self.service.check_rate_limit(scope="test", key="caller", limit=2)
        self.service.check_rate_limit(scope="test", key="caller", limit=2)
        with self.assertRaises(LicenseError) as raised:
            self.service.check_rate_limit(scope="test", key="caller", limit=2)
        self.assertEqual(raised.exception.code, "rate_limited")

    def test_checkout_rate_limit_does_not_block_other_callers(self) -> None:
        self.service.check_rate_limit(scope="test", key="a", limit=1)
        self.service.check_rate_limit(scope="test", key="b", limit=1)
        with self.assertRaises(LicenseError):
            self.service.check_rate_limit(scope="test", key="a", limit=1)


if __name__ == "__main__":
    unittest.main()
