# Moonlight Studio website + license service

This service is the public website and the activation API for Moonlight
Studio. One FastAPI process serves:

- the marketing pages (`/`, `/pricing`, `/activate`);
- hosted-checkout creation and payment callbacks;
- the activation, refresh, usage, and deactivation endpoints used by the
  desktop app;
- a small operator CLI for manual orders and support revocations.

Data lives in SQLite. In development the default payment provider is `mock`,
so the full purchase → code → activation flow works without live keys.

## Requirements

- Python 3.10+
- The repository virtual environment, or a dedicated one for the service

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r server\requirements.txt
```

## Run locally

From the repository root:

```powershell
$env:PYTHONPATH=(Get-Location).Path
$env:PAYMENT_PROVIDER='mock'
.\.venv\Scripts\python.exe -m uvicorn server.app:app --reload --port 8787
```

Open <http://127.0.0.1:8787/pricing>. The database is created automatically at
`server/data/moonlight.sqlite3` (or `MOONLIGHT_DB_PATH`).

Check readiness:

```powershell
Invoke-RestMethod http://127.0.0.1:8787/api/v1/health
```

`/api/v1/health` reports the configured provider and whether its keys are
present. `/healthz` is a bare liveness probe for a reverse proxy.

## Configuration

Copy `server/.env.example` to `server/.env` (or export the variables). The
service loads `.env` from the working directory at startup.

| Variable | Default | Purpose |
| --- | --- | --- |
| `PUBLIC_BASE_URL` | `http://127.0.0.1:8787` | Public origin used in payment callbacks. Must be HTTPS in production. |
| `WEBSITE_URL` | `PUBLIC_BASE_URL` | Origin linked from the success/activation pages. |
| `DOWNLOAD_URL` | empty | Windows installer URL. Empty hides download buttons. |
| `MOONLIGHT_DB_PATH` | `server/data/moonlight.sqlite3` | SQLite file. Use a persistent volume. |
| `PAYMENT_PROVIDER` | `mock` | `mock`, `paystack`, or `flutterwave`. |
| `PAYSTACK_SECRET_KEY` | empty | Paystack secret key. |
| `PAYSTACK_CURRENCY` | `NGN` | Paystack charge currency. |
| `FLUTTERWAVE_SECRET_KEY` | empty | Flutterwave secret key. |
| `FLUTTERWAVE_SECRET_HASH` | empty | Flutterwave dashboard webhook hash. |
| `FLUTTERWAVE_CURRENCY` | `NGN` | Flutterwave charge currency. |
| `LICENSE_SECRET` | development default | Signs entitlement tokens and seeds the code pepper. **Change before the first paid order.** |
| `CODE_PEPPER` | `LICENSE_SECRET + ':code'` | Optional separate pepper for code/claim hashes. |
| `ACTIVATION_ATTEMPTS_PER_HOUR` | `20` | Persistent per-IP activation budget. `0` disables it. |
| `CHECKOUT_ATTEMPTS_PER_HOUR` | `30` | Persistent per-IP order-creation budget. |
| `MAX_USAGE_DELTA_SECONDS` | `300` | Largest usage increment accepted from, or charged for, one heartbeat. |
| `TRUST_PROXY_HEADERS` | `0` | Read `X-Forwarded-For` / `CF-Connecting-IP`. Enable only behind a proxy that overwrites them. |
| `ALLOW_MOCK_PAYMENTS` | `0` | Permit mock payments on an HTTPS origin. Staging only. |
| `RESEND_API_KEY` | empty | Resend API key for emailing receipts & codes to buyers. |
| `RESEND_FROM_EMAIL` | `Moonlight Studio <billing@moonlight.studio>` | Sender name & verified email for Resend receipts. |

Generate a production secret once:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Changing `LICENSE_SECRET` invalidates every issued token and redemption code.
Set it before accepting real orders and keep it stable.

## Payment providers

### Mock (default)

The checkout form redirects to `/mock/pay`, where a test button marks the
order paid. The success page prints the activation code. No money moves.

Mock payments are refused when `PUBLIC_BASE_URL` is HTTPS unless
`ALLOW_MOCK_PAYMENTS=1` is set deliberately.

### Paystack

1. Set `PAYMENT_PROVIDER=paystack` and `PAYSTACK_SECRET_KEY`.
2. Set `PUBLIC_BASE_URL` to the public HTTPS origin.
3. In the Paystack dashboard, point the webhook at
   `https://your-origin/webhooks/paystack`.
4. Complete one low-value live or test transaction and confirm the code
   appears on `/checkout/success`.

Paystack webhooks are verified with the `x-paystack-signature` HMAC-SHA512
header. The browser redirect is never trusted on its own; fulfilment verifies
the transaction with the Paystack API.

### Flutterwave

1. Set `PAYMENT_PROVIDER=flutterwave`, `FLUTTERWAVE_SECRET_KEY`, and the
   dashboard's `FLUTTERWAVE_SECRET_HASH`.
2. Set `PUBLIC_BASE_URL` to the public HTTPS origin.
3. Point the webhook at `https://your-origin/webhooks/flutterwave`.
4. Complete one sandbox or live transaction end to end.

Flutterwave webhooks are verified with the `verif-hash` header, compared in
constant time.

## Operator CLI

Run from the repository root with `PYTHONPATH` set:

```powershell
$env:PYTHONPATH=(Get-Location).Path

# Create/upgrade the schema
.\.venv\Scripts\python.exe -m server.admin init-db

# Create a paid manual order and print its code
.\.venv\Scripts\python.exe -m server.admin create-code --plan creator --term yearly --email buyer@example.com

# Inspect recent orders
.\.venv\Scripts\python.exe -m server.admin list-orders

# Re-print a paid order's code
.\.venv\Scripts\python.exe -m server.admin show-code --reference ml_...

# Revoke one device permanently (support action, not user "remove device")
.\.venv\Scripts\python.exe -m server.admin revoke --entitlement-id ...
```

`revoke` is final for that entitlement. The user-facing
`POST /api/v1/deactivate` marks a device `removed` and frees its slot; it
cannot undo a support revocation.

## API summary

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/plans` | Plan catalogue with monthly/six-month/yearly prices. |
| `POST` | `/api/v1/activate` | Bind a code to a device; returns a signed token and usage. |
| `POST` | `/api/v1/refresh` | Re-check an active token and return fresh usage. |
| `POST` | `/api/v1/usage` | Idempotent usage heartbeat. |
| `POST` | `/api/v1/deactivate` | Remove this device and free its slot. |
| `GET` | `/api/v1/health` | Provider readiness and server time. |

Activation is limited by a persistent per-IP budget. Usage increments are
clamped and also measured against the server-side heartbeat gap, so a client
that reports zero still accrues time while it keeps checking in.

## Database

SQLite tables: `orders`, `subscription_codes`, `entitlements`, `usage_daily`,
`usage_events`, `webhook_events`, `rate_limit_events`.

- Codes and claims are stored only as HMAC hashes.
- One code can have several device entitlements, bounded by the plan's
  `max_devices`.
- Daily usage is pooled per code, not per device.
- Usage resets at 00:00 UTC.
- `Database.initialize()` migrates older schemas on startup and is safe to run
  repeatedly.

Back up the SQLite file before deploying a schema change. With WAL enabled,
copy the database with SQLite's backup API or stop the service first.

## Tests

```powershell
$env:PYTHONPATH=(Get-Location).Path
.\.venv\Scripts\python.exe -m unittest discover -s server/tests
```

The suite covers checkout, webhook signatures, multi-device activation,
shared usage, deactivation/rejoin, support revocation, rate limiting, and the
production configuration guards.

## Deployment checklist

- [ ] `PUBLIC_BASE_URL` and `WEBSITE_URL` are the real HTTPS origin.
- [ ] `PAYMENT_PROVIDER` is `paystack` or `flutterwave`, with keys present.
- [ ] `LICENSE_SECRET` is a fresh random value, not the repository default.
- [ ] `ALLOW_MOCK_PAYMENTS` is unset.
- [ ] `DOWNLOAD_URL` points at the published installer, or remains empty.
- [ ] `MOONLIGHT_DB_PATH` is on persistent storage and backed up.
- [ ] `TRUST_PROXY_HEADERS=1` only behind a proxy that overwrites
      `X-Forwarded-For`.
- [ ] `uvicorn` runs behind HTTPS (Caddy, nginx, Cloudflare, etc.).
- [ ] One live transaction reaches `/checkout/success` and activates in the app.

## Security notes

See [`docs/licensing-security.md`](../docs/licensing-security.md) for the
threat model and residual risks. In short:

- The server refuses to start on HTTPS with mock payments or the default
  signing secret.
- Webhooks are signature-verified; browser callbacks are re-verified with the
  provider API.
- Activation rates are stored in SQLite, so restarts and multiple workers do
  not reset them.
- A local desktop app can always be patched by a determined attacker; the
  goal is to stop casual code sharing and under-reporting, not to provide
  hardware DRM.
