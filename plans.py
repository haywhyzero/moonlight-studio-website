"""Plan catalogue and term pricing.

Prices are stored in kobo (1 NGN = 100 kobo) to avoid floating point money.
Only the monthly price is configured; the longer terms apply a discount.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

TERMS: dict[str, dict[str, object]] = {
    "monthly": {"months": 1, "discount_percent": 0, "label": "Monthly"},
    "six_months": {"months": 6, "discount_percent": 15, "label": "6 months"},
    "yearly": {"months": 12, "discount_percent": 25, "label": "12 months"},
    "single_use": {"months": 1, "discount_percent": 0, "label": "Single Pass"},
}

PLAN_TERMS = tuple(TERMS.keys())


@dataclass(frozen=True)
class Plan:
    id: str
    name: str
    tagline: str
    monthly_price_kobo: int
    daily_hours: int
    max_devices: int
    max_width: int
    max_height: int
    max_fps: int
    cloud_minutes: int
    features: tuple[str, ...] = field(default_factory=tuple)
    recommended: bool = False
    is_single_use: bool = False

    def price_for(self, term: str) -> int:
        if self.is_single_use:
            return self.monthly_price_kobo
        spec = TERMS.get(term)
        if spec is None:
            raise ValueError(f"Unknown term '{term}'.")
        months = int(spec["months"])
        discount = int(spec["discount_percent"])
        gross = self.monthly_price_kobo * months
        discounted = gross * (100 - discount)
        # Keep every term price a whole naira (100 kobo) so the website and
        # checkout never display an odd .90 remainder.
        return ((discounted + 5_000) // 10_000) * 100

    def months_for(self, term: str) -> int:
        if self.is_single_use:
            return 1
        spec = TERMS.get(term)
        if spec is None:
            raise ValueError(f"Unknown term '{term}'.")
        return int(spec["months"])


PLANS: dict[str, Plan] = {
    "starter": Plan(
        id="starter",
        name="Starter",
        tagline="Private studio time for occasional calls.",
        monthly_price_kobo=15_000_000,  # NGN 150,000
        daily_hours=2,
        max_devices=1,
        max_width=1280,
        max_height=720,
        max_fps=24,
        cloud_minutes=0,
        features=(
            "2 hours of studio time per day",
            "720p / 24 fps output",
            "1 device",
            "Local engines and live DSP voice effects",
            "Virtual camera and VB-CABLE microphone output",
        ),
    ),
    "creator": Plan(
        id="creator",
        name="Creator",
        tagline="For creators who go live every day.",
        monthly_price_kobo=30_000_000,  # NGN 300,000
        daily_hours=4,
        max_devices=2,
        max_width=1920,
        max_height=1080,
        max_fps=30,
        cloud_minutes=60,
        features=(
            "4 hours of studio time per day",
            "1080p / 30 fps output",
            "2 devices sharing one daily pool",
            "Every local engine, including the voice library",
            "60 cloud GPU minutes each month",
            "Priority email support",
        ),
        recommended=True,
    ),
    "studio": Plan(
        id="studio",
        name="Studio",
        tagline="For professional production days.",
        monthly_price_kobo=60_000_000,  # NGN 600,000
        daily_hours=8,
        max_devices=3,
        max_width=1920,
        max_height=1080,
        max_fps=60,
        cloud_minutes=240,
        features=(
            "8 hours of studio time per day",
            "1080p / 60 fps output",
            "3 devices sharing one daily pool",
            "All local and cloud engines",
            "240 cloud GPU minutes each month",
            "Priority support and early features",
        ),
    ),
    # ── Single-use Buds passes (private /buds page only) ──────────────────────
    "bud_spark": Plan(
        id="bud_spark",
        name="Spark Pass",
        tagline="One hour of studio magic.",
        monthly_price_kobo=1_000_000,  # NGN 10,000
        daily_hours=1,
        max_devices=1,
        max_width=1280,
        max_height=720,
        max_fps=24,
        cloud_minutes=10,
        features=(
            "1 hour total studio time (lifetime)",
            "720p / 24 fps output",
            "1 device",
            "10 cloud GPU minutes",
            "Real MeanVC2 & Beatrice v2 voice engines",
        ),
        is_single_use=True,
    ),
    "bud_glow": Plan(
        id="bud_glow",
        name="Glow Pass",
        tagline="Two hours, the full experience.",
        monthly_price_kobo=2_500_000,  # NGN 25,000
        daily_hours=2,
        max_devices=1,
        max_width=1920,
        max_height=1080,
        max_fps=30,
        cloud_minutes=10,
        features=(
            "2 hours total studio time (lifetime)",
            "1080p / 30 fps output",
            "1 device",
            "10 cloud GPU minutes",
            "Real MeanVC2 & Beatrice v2 voice engines",
        ),
        is_single_use=True,
    ),
    "bud_prime": Plan(
        id="bud_prime",
        name="Prime Pass",
        tagline="Two hours, premium output quality.",
        monthly_price_kobo=5_000_000,  # NGN 50,000
        daily_hours=2,
        max_devices=1,
        max_width=1920,
        max_height=1080,
        max_fps=60,
        cloud_minutes=10,
        features=(
            "2 hours total studio time (lifetime)",
            "1080p / 60 fps output",
            "1 device",
            "10 cloud GPU minutes",
            "Priority cloud processing",
            "Real MeanVC2 & Beatrice v2 voice engines",
        ),
        recommended=True,
        is_single_use=True,
    ),
}


def get_plan(plan_id: str) -> Plan | None:
    return PLANS.get(plan_id.strip().lower())


def price_ngn(kobo: int) -> str:
    naira = kobo / 100
    if naira == int(naira):
        return f"₦{int(naira):,}"
    return f"₦{naira:,.2f}"


def add_months(start: datetime, months: int) -> datetime:
    """Add calendar months, clamping to the last day of the target month."""
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    year = start.year + (start.month - 1 + months) // 12
    month = (start.month - 1 + months) % 12 + 1
    # Calendar-month arithmetic without dateutil.
    day = start.day
    while day > 1:
        try:
            return start.replace(year=year, month=month, day=day)
        except ValueError:
            day -= 1
    return start.replace(year=year, month=month, day=1)
