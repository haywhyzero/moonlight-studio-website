"""FastAPI application for the Moonlight Studio website and license API.

Run locally:
    uvicorn server.app:app --reload --port 8787

The default configuration uses the `mock` payment provider so the full
purchase -> code -> activation flow can be exercised without live keys.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

try:
    from .config import SERVER_DIR, Settings, load_settings
    from .db import Database
    from .payments import PaymentError, PaymentVerification, build_payment_provider
    from .plans import TERMS, get_plan, price_ngn
    from .security import iso, utcnow
    from .service import LicenseError, LicenseService
except (ImportError, ValueError):
    from config import SERVER_DIR, Settings, load_settings
    from db import Database
    from payments import PaymentError, PaymentVerification, build_payment_provider
    from plans import TERMS, get_plan, price_ngn
    from security import iso, utcnow
    from service import LicenseError, LicenseService

TEMPLATES_DIR = SERVER_DIR / "templates"
STATIC_DIR = SERVER_DIR / "static"


class ActivationRequest(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    deviceId: str = Field(min_length=1, max_length=128)
    deviceName: str = Field(default="", max_length=120)


class RefreshRequest(BaseModel):
    token: str = Field(min_length=1, max_length=4096)
    deviceId: str = Field(min_length=1, max_length=128)


class UsageRequest(BaseModel):
    token: str = Field(min_length=1, max_length=4096)
    deviceId: str = Field(min_length=1, max_length=128)
    eventId: str = Field(min_length=1, max_length=96)
    seconds: int = Field(ge=0, le=3600)


class DeactivateRequest(BaseModel):
    token: str = Field(min_length=1, max_length=4096)
    deviceId: str = Field(min_length=1, max_length=128)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    settings.validate_for_startup()
    database = Database(settings.database_path)
    database.initialize()
    service = LicenseService(database, settings)

    app = FastAPI(
        title="Moonlight Studio License Service",
        version="1.0.0",
        docs_url="/api/docs" if not settings.is_production else None,
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.database = database
    app.state.service = service

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.globals["price_ngn"] = price_ngn
    templates.env.globals["now"] = lambda: utcnow()
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    def context(request: Request, **extra: Any) -> dict[str, Any]:
        ready, reason = settings.payment_ready()
        return {
            "request": request,
            "plans": service.plan_payloads(),
            "terms": TERMS,
            "download_url": settings.download_url,
            "payment_provider": settings.payment_provider,
            "payment_ready": ready,
            "payment_reason": reason,
            "website_url": settings.website_url,
            "year": datetime.now(timezone.utc).year,
            "current_path": request.url.path,
            **extra,
        }

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self'; script-src 'self'; "
            "img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'",
        )
        if settings.is_production:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response

    @app.exception_handler(LicenseError)
    async def license_error_handler(request: Request, exc: LicenseError):
        if request.url.path.startswith("/api/"):
            return JSONResponse(
                {"detail": exc.message, "code": exc.code},
                status_code=exc.status_code,
            )
        return templates.TemplateResponse(
            "error.html",
            context(
                request,
                title="Something went wrong",
                message=exc.message,
                status_code=exc.status_code,
            ),
            status_code=exc.status_code,
        )

    @app.exception_handler(PaymentError)
    async def payment_error_handler(request: Request, exc: PaymentError):
        if request.url.path.startswith("/api/"):
            return JSONResponse(
                {"detail": str(exc), "code": "payment_error"}, status_code=502
            )
        return templates.TemplateResponse(
            "error.html",
            context(
                request,
                title="Payment unavailable",
                message=str(exc),
                status_code=502,
            ),
            status_code=502,
        )

    # ---------------------------------------------------------------- pages
    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return templates.TemplateResponse("index.html", context(request))

    @app.get("/pricing", response_class=HTMLResponse)
    async def pricing(request: Request):
        return templates.TemplateResponse("pricing.html", context(request))

    @app.get("/activate", response_class=HTMLResponse)
    async def activate_page(request: Request):
        return templates.TemplateResponse("activate.html", context(request))

    @app.get("/download/windows")
    async def download_windows():
        if not settings.download_url:
            raise LicenseError(
                "The Windows build is not published yet. Check back soon.",
                status_code=404,
                code="download_unavailable",
            )
        return RedirectResponse(settings.download_url, status_code=307)

    @app.post("/checkout")
    async def checkout(
        request: Request,
        plan_id: str = Form(...),
        term: str = Form(...),
        email: str = Form(...),
    ):
        service.check_rate_limit(
            scope="checkout",
            key=_client_ip(request, settings),
            limit=settings.checkout_attempts_per_hour,
        )
        order, claim = service.create_order(
            plan_id=plan_id,
            term=term,
            email=email,
            provider=settings.payment_provider,
        )
        try:
            url = service.checkout_url(order, claim)
        except LicenseError:
            # Keep the order out of the way if the provider is unavailable.
            raise
        return RedirectResponse(url, status_code=303)

    @app.get("/mock/pay", response_class=HTMLResponse)
    async def mock_pay_page(request: Request, reference: str, claim: str):
        if not settings.is_mock_payments:
            raise LicenseError("Mock payments are disabled.", status_code=404)
        order = service.get_order(reference)
        if order is None or _order_for_claim(service, claim, reference) is None:
            raise LicenseError("Order not found.", status_code=404)
        return templates.TemplateResponse(
            "mock_pay.html",
            context(
                request,
                title="Test payment",
                order=order,
                plan=get_plan(order["plan_id"]),
                claim=claim,
                reference=reference,
            ),
        )

    @app.post("/mock/pay")
    async def mock_pay(reference: str = Form(...), claim: str = Form(...)):
        if not settings.is_mock_payments:
            raise LicenseError("Mock payments are disabled.", status_code=404)
        order = service.get_order(reference)
        if order is None or _order_for_claim(service, claim, reference) is None:
            raise LicenseError("Order not found.", status_code=404)
        service.mark_paid(
            PaymentVerification(
                success=True,
                reference=reference,
                amount_kobo=0,
                currency=order["currency"],
                event_id=f"mock:{reference}",
                raw={"provider": "mock"},
            ),
            provider_name="mock",
        )
        return RedirectResponse(
            f"/checkout/success?claim={claim}", status_code=303
        )

    @app.get("/payment/callback")
    async def payment_callback(
        request: Request,
        provider: str = "",
        claim: str = "",
        reference: str = "",
        trxref: str = "",
        tx_ref: str = "",
    ):
        chosen = provider or settings.payment_provider
        if chosen != settings.payment_provider:
            raise LicenseError("Payment provider mismatch.", code="provider_mismatch")
        resolved = reference or trxref or tx_ref
        if not resolved:
            raise LicenseError("The payment reference is missing.", code="missing_reference")
        try:
            service.verify_and_fulfill(
                reference=resolved, provider_name=settings.payment_provider
            )
        except LicenseError as exc:
            if claim:
                return RedirectResponse(
                    f"/checkout/success?claim={claim}", status_code=303
                )
            raise exc
        if not claim:
            raise LicenseError(
                "The order claim is missing. Open the link from checkout again.",
                code="missing_claim",
            )
        return RedirectResponse(f"/checkout/success?claim={claim}", status_code=303)

    @app.get("/checkout/success", response_class=HTMLResponse)
    async def checkout_success(request: Request, claim: str):
        order = service.get_order_by_claim(claim)
        if order is None:
            raise LicenseError(
                "This checkout link is invalid or has expired.",
                status_code=404,
                code="unknown_claim",
            )
        if order["status"] != "paid" and not settings.is_mock_payments:
            # The webhook may be a little faster than the browser redirect.
            try:
                service.verify_and_fulfill(
                    reference=order["reference"],
                    provider_name=settings.payment_provider,
                )
                order = service.get_order_by_claim(claim) or order
            except LicenseError:
                pass
        paid = order["status"] == "paid"
        code = service.code_for_order(order) if paid else ""
        return templates.TemplateResponse(
            "success.html",
            context(
                request,
                title="Your subscription code" if paid else "Confirming payment",
                order=order,
                plan=get_plan(order["plan_id"]),
                code=code,
                paid=paid,
                claim=claim,
            ),
            status_code=200 if paid else 202,
        )

    # ------------------------------------------------------------------ api
    @app.get("/api/plans")
    async def api_plans():
        return {"plans": service.plan_payloads()}

    @app.post("/api/v1/activate")
    async def api_activate(payload: ActivationRequest, request: Request):
        service.check_rate_limit(
            scope="activate",
            key=_client_ip(request, settings),
            limit=settings.activation_attempts_per_hour,
        )
        return service.activate(
            code=payload.code,
            device_id=payload.deviceId,
            device_name=payload.deviceName,
        )

    @app.post("/api/v1/refresh")
    async def api_refresh(payload: RefreshRequest):
        return service.refresh(token=payload.token, device_id=payload.deviceId)

    @app.post("/api/v1/usage")
    async def api_usage(payload: UsageRequest):
        return service.record_usage(
            token=payload.token,
            device_id=payload.deviceId,
            event_id=payload.eventId,
            seconds=payload.seconds,
        )

    @app.post("/api/v1/deactivate")
    async def api_deactivate(payload: DeactivateRequest):
        """Remove this device from the subscription and free its slot."""
        return service.deactivate(token=payload.token, device_id=payload.deviceId)

    @app.get("/api/v1/health")
    async def api_health():
        ready, reason = settings.payment_ready()
        return {
            "status": "ok",
            "paymentProvider": settings.payment_provider,
            "paymentReady": ready,
            "paymentReason": reason,
            "time": iso(utcnow()),
        }

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    # ------------------------------------------------------------- webhooks
    @app.post("/webhooks/paystack")
    async def paystack_webhook(request: Request):
        if settings.payment_provider != "paystack":
            raise LicenseError("Paystack is not enabled.", status_code=404)
        body = await request.body()
        provider = build_payment_provider(settings)
        verification = provider.parse_webhook(request.headers, body)
        if verification is None:
            return {"status": "ignored"}
        service.record_webhook(
            event_id=f"paystack:{verification.event_id}",
            provider="paystack",
            payload=body,
        )
        service.mark_paid(verification, provider_name="paystack")
        return {"status": "ok"}

    @app.post("/webhooks/flutterwave")
    async def flutterwave_webhook(request: Request):
        if settings.payment_provider != "flutterwave":
            raise LicenseError("Flutterwave is not enabled.", status_code=404)
        body = await request.body()
        provider = build_payment_provider(settings)
        verification = provider.parse_webhook(request.headers, body)
        if verification is None:
            return {"status": "ignored"}
        service.record_webhook(
            event_id=f"flutterwave:{verification.event_id}",
            provider="flutterwave",
            payload=body,
        )
        service.mark_paid(verification, provider_name="flutterwave")
        return {"status": "ok"}

    return app


def _client_ip(request: Request, settings: Settings) -> str:
    """Best-effort caller identity for throttling.

    `X-Forwarded-For` is only read when TRUST_PROXY_HEADERS is on, because a
    direct client can set that header freely. Enable it only behind a proxy
    that overwrites the header with the real peer address.
    """
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            first = forwarded.split(",")[0].strip()
            if first:
                return first[:64]
        cloudflare = request.headers.get("cf-connecting-ip", "").strip()
        if cloudflare:
            return cloudflare[:64]
    return request.client.host if request.client else "unknown"


def _order_for_claim(
    service: LicenseService, claim: str, reference: str
) -> dict[str, Any] | None:
    """Require a mock-payment link's claim to belong to its reference."""
    order = service.get_order_by_claim(claim)
    if order is None or order["reference"] != reference:
        return None
    return order


app = create_app()
