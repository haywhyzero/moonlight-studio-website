"""Runtime configuration for the Moonlight Studio license service.

Everything is environment-driven so the same code runs locally, in staging,
and in production without editing source. Copy `.env.example` to `.env` for
local development.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

SERVER_DIR = Path(__file__).resolve().parent
REPO_ROOT = SERVER_DIR.parent

DEFAULT_LICENSE_SECRET = "dev-only-change-this-license-secret"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    """Configuration values shared by the web app and the API."""

    database_path: Path
    public_base_url: str = "http://127.0.0.1:8787"
    download_url: str = ""
    website_url: str = "http://127.0.0.1:8787"

    payment_provider: str = "mock"
    paystack_secret_key: str = ""
    paystack_currency: str = "NGN"
    flutterwave_secret_key: str = ""
    flutterwave_secret_hash: str = ""
    flutterwave_currency: str = "NGN"

    # Resend email receipt configuration (production only).
    resend_api_key: str = ""
    resend_from_email: str = "Moonlight Studio <billing@moonlight.studio>"

    # Used to sign entitlement tokens and to derive redemption codes. Change
    # this once, before the first paid order, and keep it stable.
    license_secret: str = DEFAULT_LICENSE_SECRET
    code_pepper: str = ""

    # Operational limits.
    activation_attempts_per_hour: int = 20
    checkout_attempts_per_hour: int = 30
    max_usage_delta_seconds: int = 300
    trust_proxy_headers: bool = False

    # Local testing only. Production refuses to start with the mock provider
    # unless this is explicitly enabled.
    allow_mock_payments: bool = False

    extra: dict[str, str] = field(default_factory=dict)

    @property
    def resolved_code_pepper(self) -> str:
        return self.code_pepper or f"{self.license_secret}:code"

    @property
    def is_mock_payments(self) -> bool:
        return self.payment_provider == "mock"

    @property
    def is_production(self) -> bool:
        return self.public_base_url.startswith("https://")

    @property
    def uses_default_license_secret(self) -> bool:
        return self.license_secret == DEFAULT_LICENSE_SECRET

    def validate_for_startup(self) -> None:
        """Fail fast on unsafe production configuration.

        A mock provider in production lets anyone mark an order paid, and the
        published default signing secret lets anyone mint entitlement tokens.
        Both are development conveniences, so a production origin must opt out
        of or replace them explicitly.
        """
        if not self.is_production:
            return
        if self.is_mock_payments and not self.allow_mock_payments:
            raise RuntimeError(
                "Refusing to start: PAYMENT_PROVIDER=mock with an HTTPS "
                "PUBLIC_BASE_URL. Configure paystack or flutterwave, or set "
                "ALLOW_MOCK_PAYMENTS=1 for a deliberate staging environment."
            )
        if self.uses_default_license_secret:
            raise RuntimeError(
                "Refusing to start: LICENSE_SECRET is still the development "
                "default. Generate one with "
                'python -c "import secrets; print(secrets.token_urlsafe(48))".'
            )

    @property
    def callback_base(self) -> str:
        return self.public_base_url.rstrip("/")

    def payment_ready(self) -> tuple[bool, str]:
        """Return (ready, reason) for the configured payment provider."""
        provider = self.payment_provider.lower()
        if provider == "mock":
            return True, ""
        if provider == "paystack":
            if not self.paystack_secret_key:
                return False, "PAYSTACK_SECRET_KEY is not configured."
            return True, ""
        if provider == "flutterwave":
            if not self.flutterwave_secret_key:
                return False, "FLUTTERWAVE_SECRET_KEY is not configured."
            if not self.flutterwave_secret_hash:
                return False, "FLUTTERWAVE_SECRET_HASH is not configured."
            return True, ""
        return False, f"Unknown PAYMENT_PROVIDER '{self.payment_provider}'."

    @classmethod
    def from_env(cls) -> "Settings":
        default_db = SERVER_DIR / "data" / "moonlight.sqlite3"
        return cls(
            database_path=Path(_env("MOONLIGHT_DB_PATH", str(default_db))),
            public_base_url=_env(
                "PUBLIC_BASE_URL", "http://127.0.0.1:8787"
            ).rstrip("/"),
            download_url=_env("DOWNLOAD_URL", ""),
            website_url=_env("WEBSITE_URL", _env("PUBLIC_BASE_URL", "")),
            payment_provider=_env("PAYMENT_PROVIDER", "mock").lower(),
            paystack_secret_key=_env("PAYSTACK_SECRET_KEY"),
            paystack_currency=_env("PAYSTACK_CURRENCY", "NGN"),
            flutterwave_secret_key=_env("FLUTTERWAVE_SECRET_KEY"),
            flutterwave_secret_hash=_env("FLUTTERWAVE_SECRET_HASH"),
            flutterwave_currency=_env("FLUTTERWAVE_CURRENCY", "NGN"),
            resend_api_key=_env("RESEND_API_KEY"),
            resend_from_email=_env(
                "RESEND_FROM_EMAIL", "Moonlight Studio <billing@moonlight.studio>"
            ),
            license_secret=_env("LICENSE_SECRET", DEFAULT_LICENSE_SECRET),
            code_pepper=_env("CODE_PEPPER"),
            activation_attempts_per_hour=_env_int(
                "ACTIVATION_ATTEMPTS_PER_HOUR", 20
            ),
            checkout_attempts_per_hour=_env_int(
                "CHECKOUT_ATTEMPTS_PER_HOUR", 30
            ),
            max_usage_delta_seconds=_env_int("MAX_USAGE_DELTA_SECONDS", 300),
            trust_proxy_headers=_env("TRUST_PROXY_HEADERS", "0")
            in {"1", "true", "yes"},
            allow_mock_payments=_env("ALLOW_MOCK_PAYMENTS", "0")
            in {"1", "true", "yes"},
        )


def load_settings() -> Settings:
    return Settings.from_env()
