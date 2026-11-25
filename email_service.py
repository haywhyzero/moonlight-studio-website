"""Email receipt delivery service via Resend.

Only sends receipts in production (when not using mock payments) and when a
valid RESEND_API_KEY is configured. Never throws exceptions to caller to avoid
interrupting payment fulfillment or user redirect flows.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

try:
    from .config import Settings
    from .plans import Plan, price_ngn
except (ImportError, ValueError):
    from config import Settings
    from plans import Plan, price_ngn

logger = logging.getLogger("moonlight.email")


def build_receipt_html(
    *,
    order: dict[str, Any],
    plan: Plan,
    code: str,
    settings: Settings,
) -> str:
    amount_str = price_ngn(order.get("amount_kobo", 0))
    term_label = str(order.get("term", "")).replace("_", " ").capitalize()
    download_url = settings.download_url or f"{settings.public_base_url}/download/windows"

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Your Moonlight Studio Subscription</title>
</head>
<body style="margin: 0; padding: 0; background-color: #07070f; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; color: #f0f2ff;">
  <table width="100%" border="0" cellspacing="0" cellpadding="0" style="background-color: #07070f; padding: 40px 20px;">
    <tr>
      <td align="center">
        <table width="100%" border="0" cellspacing="0" cellpadding="0" style="max-width: 580px; background-color: #0e0f1d; border: 1px solid #1f233d; border-radius: 12px; overflow: hidden; box-shadow: 0 10px 30px rgba(0,0,0,0.5);">
          
          <!-- Header -->
          <tr>
            <td style="padding: 32px 32px 24px 32px; border-bottom: 1px solid #1a1c30; background: linear-gradient(180deg, #13152c 0%, #0e0f1d 100%);">
              <div style="font-size: 22px; font-weight: 700; color: #ffffff; letter-spacing: -0.5px;">
                Moonlight <span style="font-size: 13px; font-weight: 800; color: #8c82ff; text-transform: uppercase; letter-spacing: 1px;">Studio</span>
              </div>
              <div style="margin-top: 8px; color: #2ecc71; font-size: 14px; font-weight: 600;">
                ✓ Payment Confirmed
              </div>
            </td>
          </tr>

          <!-- Main Content -->
          <tr>
            <td style="padding: 32px;">
              <h1 style="margin: 0 0 12px 0; font-size: 24px; color: #ffffff; font-weight: 700;">
                Here is your subscription code
              </h1>
              <p style="margin: 0 0 24px 0; font-size: 15px; color: #a4adcc; line-height: 1.5;">
                Thank you for your purchase. Enter this code into Moonlight Studio on your PC to activate your plan.
              </p>

              <!-- Code Box -->
              <div style="background-color: #151833; border: 1px solid #373d6b; border-radius: 8px; padding: 20px; text-align: center; margin-bottom: 28px;">
                <div style="font-size: 12px; text-transform: uppercase; color: #8e99c7; letter-spacing: 1.5px; margin-bottom: 8px; font-weight: 600;">
                  Activation Code
                </div>
                <div style="font-family: 'Consolas', 'Courier New', monospace; font-size: 26px; font-weight: 700; color: #a78bfa; letter-spacing: 2px;">
                  {code}
                </div>
              </div>

              <!-- Order Summary Table -->
              <table width="100%" border="0" cellspacing="0" cellpadding="0" style="margin-bottom: 28px; border-collapse: collapse;">
                <tr>
                  <td style="padding: 10px 0; border-bottom: 1px solid #1a1d36; color: #8e99c7; font-size: 14px;">Plan</td>
                  <td align="right" style="padding: 10px 0; border-bottom: 1px solid #1a1d36; color: #ffffff; font-size: 14px; font-weight: 600;">{plan.name}</td>
                </tr>
                <tr>
                  <td style="padding: 10px 0; border-bottom: 1px solid #1a1d36; color: #8e99c7; font-size: 14px;">Billing Term</td>
                  <td align="right" style="padding: 10px 0; border-bottom: 1px solid #1a1d36; color: #ffffff; font-size: 14px; font-weight: 600;">{term_label}</td>
                </tr>
                <tr>
                  <td style="padding: 10px 0; border-bottom: 1px solid #1a1d36; color: #8e99c7; font-size: 14px;">Amount Paid</td>
                  <td align="right" style="padding: 10px 0; border-bottom: 1px solid #1a1d36; color: #ffffff; font-size: 14px; font-weight: 700;">{amount_str}</td>
                </tr>
                <tr>
                  <td style="padding: 10px 0; border-bottom: 1px solid #1a1d36; color: #8e99c7; font-size: 14px;">Daily Studio Hours</td>
                  <td align="right" style="padding: 10px 0; border-bottom: 1px solid #1a1d36; color: #ffffff; font-size: 14px; font-weight: 600;">{plan.daily_hours} hrs / day</td>
                </tr>
                <tr>
                  <td style="padding: 10px 0; border-bottom: 1px solid #1a1d36; color: #8e99c7; font-size: 14px;">Device Limit</td>
                  <td align="right" style="padding: 10px 0; border-bottom: 1px solid #1a1d36; color: #ffffff; font-size: 14px; font-weight: 600;">Up to {plan.max_devices} device{'s' if plan.max_devices != 1 else ''}</td>
                </tr>
                <tr>
                  <td style="padding: 10px 0; border-bottom: 1px solid #1a1d36; color: #8e99c7; font-size: 14px;">Reference</td>
                  <td align="right" style="padding: 10px 0; border-bottom: 1px solid #1a1d36; color: #7680a6; font-family: monospace; font-size: 12px;">{order.get("reference", "")}</td>
                </tr>
              </table>

              <!-- Quick Start Steps -->
              <div style="background-color: #101226; border-radius: 8px; padding: 20px; margin-bottom: 28px;">
                <div style="color: #ffffff; font-weight: 600; font-size: 14px; margin-bottom: 12px;">
                  How to activate:
                </div>
                <div style="color: #a4adcc; font-size: 13px; line-height: 1.6;">
                  1. Launch Moonlight Studio on your Windows PC.<br>
                  2. Paste your 12-character code when prompted.<br>
                  3. Your studio limits, active term, and features will unlock immediately.
                </div>
              </div>

              <!-- Download CTA -->
              <table width="100%" border="0" cellspacing="0" cellpadding="0">
                <tr>
                  <td align="center">
                    <a href="{download_url}" style="display: inline-block; background-color: #6d28d9; color: #ffffff; text-decoration: none; font-size: 15px; font-weight: 600; padding: 12px 28px; border-radius: 6px; box-shadow: 0 4px 12px rgba(109,40,217,0.35);">
                      Download Moonlight Studio for Windows
                    </a>
                  </td>
                </tr>
              </table>

            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td style="padding: 24px 32px; background-color: #0b0c17; border-top: 1px solid #17192b; color: #616885; font-size: 12px; text-align: center; line-height: 1.5;">
              Questions or support? Reply to this email or write to <a href="mailto:support@moonlight.studio" style="color: #8c82ff; text-decoration: none;">support@moonlight.studio</a>.<br>
              © Moonlight Studio. All rights reserved.
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""


def build_receipt_text(
    *,
    order: dict[str, Any],
    plan: Plan,
    code: str,
    settings: Settings,
) -> str:
    amount_str = price_ngn(order.get("amount_kobo", 0))
    term_label = str(order.get("term", "")).replace("_", " ").capitalize()
    download_url = settings.download_url or f"{settings.public_base_url}/download/windows"

    return f"""Moonlight Studio — Subscription Receipt & Code

Thank you for your purchase! Your payment has been confirmed.

YOUR ACTIVATION CODE:
{code}

ORDER DETAILS:
- Plan: {plan.name}
- Term: {term_label}
- Amount Paid: {amount_str}
- Daily Limit: {plan.daily_hours} hours / day
- Devices Allowed: {plan.max_devices}
- Reference: {order.get("reference", "")}

HOW TO ACTIVATE:
1. Open Moonlight Studio on your Windows PC.
2. Enter your activation code above when prompted.
3. Your plan and features will unlock immediately.

Download installer: {download_url}
Support: moonlight-support@aerovyx.net
"""


def send_receipt_email(
    *,
    order: dict[str, Any],
    plan: Plan,
    code: str,
    settings: Settings,
) -> bool:
    """Send payment receipt & activation code to the buyer using Resend.

    Rules:
    - Never runs when payment_provider is 'mock' (production only).
    - Requires RESEND_API_KEY.
    - Never raises unhandled exceptions to caller.
    """
    if settings.is_mock_payments:
        logger.info("Mock payment mode: skipping receipt email.")
        return False

    api_key = settings.resend_api_key.strip()
    if not api_key:
        logger.warning(
            "RESEND_API_KEY is not configured; cannot send receipt to %s",
            order.get("email"),
        )
        return False

    recipient = str(order.get("email", "")).strip()
    if not recipient:
        logger.warning("Order %s has no recipient email.", order.get("reference"))
        return False

    subject = f"Your Moonlight Studio Code: {plan.name} ({code})"
    html_content = build_receipt_html(
        order=order, plan=plan, code=code, settings=settings
    )
    text_content = build_receipt_text(
        order=order, plan=plan, code=code, settings=settings
    )

    # 1. Try official resend SDK if installed
    try:
        import resend  # type: ignore

        resend.api_key = api_key
        params = {
            "from": settings.resend_from_email,
            "to": [recipient],
            "subject": subject,
            "html": html_content,
            "text": text_content,
        }
        resend.Emails.send(params)
        logger.info("Receipt email sent via Resend SDK to %s", recipient)
        return True
    except ImportError:
        pass
    except Exception as exc:
        logger.exception("Failed to send receipt via resend SDK: %s", exc)
        return False

    # 2. Fallback to direct Resend REST API (using requests)
    try:
        response = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": settings.resend_from_email,
                "to": [recipient],
                "subject": subject,
                "html": html_content,
                "text": text_content,
            },
            timeout=10,
        )
        if response.status_code in (200, 201):
            logger.info("Receipt email sent via Resend HTTP API to %s", recipient)
            return True
        else:
            logger.error(
                "Resend API returned status %s: %s",
                response.status_code,
                response.text,
            )
            return False
    except Exception as exc:
        logger.exception("Failed to send receipt via Resend HTTP API: %s", exc)
        return False
