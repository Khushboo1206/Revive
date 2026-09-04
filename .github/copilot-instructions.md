# Revive backend instructions

- Keep payment handling idempotent and verify Razorpay signatures before persistence.
- Keep recovery policy deterministic, bounded, and separate from agent orchestration.
- Record every recovery decision and outcome in the audit trail.
- Use UTC-aware timestamps and explicit recovery state transitions.
- Run `py -m pytest -q` before completing backend changes.
