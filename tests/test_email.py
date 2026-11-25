from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

try:
    from server.config import Settings
    from server.db import Database
    from server.email_service import build_receipt_html, build_receipt_text, send_receipt_email
    from server.plans import get_plan
    from server.service import LicenseService
except ModuleNotFoundError:
    from config import Settings
    from db import Database
    from email_service import build_receipt_html, build_receipt_text, send_receipt_email
    from plans import get_plan
    from service import LicenseService


class EmailServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings(
            database_path=Path(self.temp.name) / "test.sqlite3",
            license_secret="test-secret",
            payment_provider="paystack",
            resend_api_key="re_test_key_123",
            resend_from_email="Moonlight Studio <billing@moonlight.studio>",
        )
        self.db = Database(self.settings.database_path)
        self.db.initialize()
        self.service = LicenseService(self.db, self.settings)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_mock_payment_never_sends_email(self) -> None:
        mock_settings = Settings(
            database_path=Path(self.temp.name) / "mock.sqlite3",
            payment_provider="mock",
            resend_api_key="re_test_123",
        )
        order = {"email": "buyer@example.com", "reference": "ml_123", "amount_kobo": 30000000}
        plan = get_plan("creator")
        sent = send_receipt_email(order=order, plan=plan, code="ML-ABCD-EFGH-JKLM", settings=mock_settings)
        self.assertFalse(sent)

    def test_missing_api_key_skips_email(self) -> None:
        no_key_settings = Settings(
            database_path=Path(self.temp.name) / "no_key.sqlite3",
            payment_provider="paystack",
            resend_api_key="",
        )
        order = {"email": "buyer@example.com", "reference": "ml_123", "amount_kobo": 30000000}
        plan = get_plan("creator")
        sent = send_receipt_email(order=order, plan=plan, code="ML-ABCD-EFGH-JKLM", settings=no_key_settings)
        self.assertFalse(sent)

    def test_build_receipt_html_and_text(self) -> None:
        order = {"email": "buyer@example.com", "reference": "ml_ref_123", "term": "monthly", "amount_kobo": 15000000}
        plan = get_plan("starter")
        html = build_receipt_html(order=order, plan=plan, code="ML-1111-2222-3333", settings=self.settings)
        text = build_receipt_text(order=order, plan=plan, code="ML-1111-2222-3333", settings=self.settings)
        self.assertIn("ML-1111-2222-3333", html)
        self.assertIn("Starter", html)
        self.assertIn("ML-1111-2222-3333", text)
        self.assertIn("Starter", text)

    @patch("requests.post")
    def test_send_receipt_email_success(self, mock_post: MagicMock) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        order = {"email": "buyer@example.com", "reference": "ml_123", "amount_kobo": 30000000}
        plan = get_plan("creator")
        sent = send_receipt_email(
            order=order,
            plan=plan,
            code="ML-ABCD-EFGH-JKLM",
            settings=self.settings,
        )
        self.assertTrue(sent)
        self.assertTrue(mock_post.called)
        call_kwargs = mock_post.call_args[1]
        self.assertEqual(call_kwargs["json"]["to"], ["buyer@example.com"])
        self.assertEqual(call_kwargs["headers"]["Authorization"], "Bearer re_test_key_123")

    def test_send_receipt_if_needed_is_idempotent(self) -> None:
        import sys
        service_mod = sys.modules[LicenseService.__module__]
        with patch.object(service_mod, "send_receipt_email", return_value=True) as mock_send:
            order, claim = self.service.create_order(
                plan_id="creator",
                term="monthly",
                email="buyer@example.com",
                provider="paystack",
            )
            # Not paid yet
            self.assertFalse(self.service.send_receipt_if_needed(order))
            mock_send.assert_not_called()

            # Mark paid
            with self.db.connect() as conn:
                conn.execute("UPDATE orders SET status = 'paid' WHERE id = ?", (order["id"],))
                conn.commit()
            order["status"] = "paid"

            # First time paid -> sends
            first_call = self.service.send_receipt_if_needed(order)
            self.assertTrue(first_call)
            self.assertEqual(mock_send.call_count, 1)

            # Second time -> already sent, does not call send_receipt_email again
            second_call = self.service.send_receipt_if_needed(order)
            self.assertFalse(second_call)
            self.assertEqual(mock_send.call_count, 1)


if __name__ == "__main__":
    unittest.main()
