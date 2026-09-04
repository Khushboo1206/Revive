# Revive

AI revenue recovery backend for bounded payment-failure recovery workflows.

## Run locally

1. Create a virtual environment: `py -m venv .venv`
2. Activate it: `.venv\\Scripts\\Activate.ps1`
3. Install dependencies: `py -m pip install -e ".[test]"`
4. Copy `.env.example` to `.env` and configure Razorpay values.
5. Start PostgreSQL locally and ensure it is available at the `DATABASE_URL` configured in `.env`.
6. Start the API: `uvicorn app.main:app --reload`

Open `http://127.0.0.1:8000/docs` for the API explorer.

The first workflow accepts a verified Razorpay payment-failure webhook, persists a revenue-risk case, applies deterministic recovery policy, and records an audit event. Payment execution remains bounded behind policy approval.

## Razorpay webhook

Configure a Razorpay webhook with these values:

- URL: `https://<your-public-host>/webhooks/razorpay`
- Secret: the same value as `RAZORPAY_WEBHOOK_SECRET` in `.env`
- Events: `payment.failed` and `payment.captured`

The endpoint requires Razorpay's `X-Razorpay-Signature` header. Failed payments create an `open` case and an action; captured payments mark the matching case `recovered`. For local development, expose the API through an HTTPS tunnel and use that public URL in Razorpay.

Every verified webhook payload is stored as complete JSON in `webhook_events.payload`, including fields that are not mapped into the recovery tables.
