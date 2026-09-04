from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.db import Base
from app.models import Action, AuditLog, Case, CaseStatus, HumanReviewQueue, Promise, PromiseStatus, WebhookEvent
from app.recovery_engine import RecoveryEngine
from app.recovery_strategy import SafeRecoveryStrategy
from app.service import classify_payment_failure, process_payment_captured, process_payment_failed, process_payment_link_event, record_promise, update_promise_status


def test_payment_failure_classifier_handles_common_causes() -> None:
    assert classify_payment_failure({"method": "wallet", "wallet": "airtelmoney", "error_description": "Insufficient balance"}) == "wallet_insufficient_balance"
    assert classify_payment_failure({"method": "card", "error_description": "Card expired"}) == "card_expired"
    assert classify_payment_failure({"method": "upi", "error_description": "UPI request timed out"}) == "upi_timeout"
    assert classify_payment_failure({"method": "card", "error_description": "Fraud risk check rejected"}) == "fraud_rejection"


def test_webhook_event_table_stores_complete_payload() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    payload = '{"id":"evt_full","event":"payment.failed","payload":{"payment":{"entity":{"method":"wallet","error_description":"temporary issue"}}}}'
    with Session(engine) as db:
        db.add(WebhookEvent(razorpay_event_id="evt_full", event="payment.failed", payload=payload))
        db.commit()
        saved = db.scalar(select(WebhookEvent).where(WebhookEvent.razorpay_event_id == "evt_full"))
        assert saved is not None
        assert '"error_description":"temporary issue"' in saved.payload


def test_failed_payment_is_idempotent_and_audited() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    payload = {"id": "evt_1", "payload": {"payment": {"entity": {"id": "pay_1", "amount": 10000, "currency": "INR", "error_code": "GATEWAY_ERROR", "notes": {"reference_id": "24568"}}}}}
    with Session(engine) as db:
        first = process_payment_failed(db, payload)
        second = process_payment_failed(db, payload)
        assert first.reason == "queued_for_recovery_engine"
        assert second.reason == "duplicate_event_ignored"
        assert second.risk_id == first.risk_id
        assert first.recovery_probability == Decimal("0.00")


def test_recovery_engine_creates_retry_action() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    payload = {"id": "evt_action", "payload": {"payment": {"entity": {"id": "pay_action", "amount": 10000, "customer_id": "cust_1", "error_code": "GATEWAY_ERROR"}}}}
    with Session(engine) as db:
        process_payment_failed(db, payload)
        RecoveryEngine(SafeRecoveryStrategy(), retry_outcome=lambda _: "retry_again").run_cycle(db)
        action = db.scalar(select(Action).where(Action.case_id == "pay_action"))
        assert action.action_type == "no_action"
        assert action.channel is None
        assert action.message is None


def test_retries_are_counted_and_stopped_at_policy_limit() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        for attempt in range(4):
            payload = {"id": f"evt_retry_{attempt}", "payload": {"payment": {"entity": {"id": "pay_retry", "amount": 10000, "error_code": "GATEWAY_ERROR"}}}}
            result = process_payment_failed(db, payload)
        case = db.get(Case, "pay_retry")
        assert case.retry_count == 0
        assert case.next_retry_at is None
        assert result.reason == "queued_for_recovery_engine"
        assert db.scalar(select(Action).where(Action.case_id == case.id)) is None


def test_capture_recovers_case_and_audits_transition() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    failed = {"id": "evt_failed", "payload": {"payment": {"entity": {"id": "pay_2", "amount": 10000, "error_code": "GATEWAY_ERROR"}}}}
    captured = {"id": "evt_captured", "payload": {"payment": {"entity": {"id": "pay_2"}}}}
    with Session(engine) as db:
        process_payment_failed(db, failed)
        result = process_payment_captured(db, captured)
        assert result is not None
        assert result.status == CaseStatus.RECOVERED
        assert db.scalar(select(func.count(AuditLog.id)).where(AuditLog.case_id == "pay_2")) == 2


def test_payment_link_lifecycle_updates_case() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    expired = {"id": "evt_link_expired", "event": "payment_link.expired", "payload": {"payment_link": {"entity": {"id": "plink_1", "amount": 100000}}}}
    paid = {"id": "evt_link_paid", "event": "payment_link.paid", "payload": {"payment_link": {"entity": {"id": "plink_1", "amount": 100000}}}}
    with Session(engine) as db:
        expired_result = process_payment_link_event(db, expired)
        paid_result = process_payment_link_event(db, paid)
        assert expired_result.status == CaseStatus.CLOSED_LOST
        assert paid_result.status == CaseStatus.RECOVERED


def test_duplicate_payment_backfills_missing_details() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    first_payload = {"id": "evt_backfill", "payload": {"payment": {"entity": {"id": "pay_backfill", "amount": 65200, "error_code": "BAD_REQUEST_ERROR"}}}}
    second_payload = {"id": "evt_backfill_2", "payload": {"payment": {"entity": {"id": "pay_backfill", "amount": 65200, "method": "card", "order_id": "order_backfill", "error_code": "BAD_REQUEST_ERROR", "error_description": "Payment failed", "error_source": "gateway", "error_step": "payment_authorization"}}}}
    with Session(engine) as db:
        process_payment_failed(db, first_payload)
        process_payment_failed(db, second_payload)
        case = db.get(Case, "pay_backfill")
        assert case.payment_method == "card"
        assert case.order_id == "order_backfill"
        assert case.error_description == "Payment failed"


def test_failed_payment_stores_payment_details() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    payload = {"id": "evt_details", "payload": {"payment": {"entity": {"id": "pay_details", "amount": 65200, "method": "card", "order_id": "order_123", "error_code": "BAD_REQUEST_ERROR", "error_description": "Payment failed", "error_source": "gateway", "error_step": "payment_authorization"}}}}
    with Session(engine) as db:
        process_payment_failed(db, payload)
        case = db.get(Case, "pay_details")
        assert case.payment_method == "card"
        assert case.order_id == "order_123"
        assert case.error_description == "Payment failed"
        assert case.error_source == "gateway"
        assert case.error_step == "payment_authorization"


def test_broken_promise_escalates_case_and_creates_queue_entry() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    failed = {"id": "evt_promise", "payload": {"payment": {"entity": {"id": "pay_3", "amount": 10000, "error_code": "GATEWAY_ERROR"}}}}
    with Session(engine) as db:
        process_payment_failed(db, failed)
        promise = record_promise(db, "pay_3", datetime.now(timezone.utc))
        update_promise_status(db, promise.id, PromiseStatus.BROKEN)
        assert db.get(Promise, promise.id).status == PromiseStatus.BROKEN
        assert db.scalar(select(HumanReviewQueue).where(HumanReviewQueue.case_id == "pay_3")) is not None
        assert db.scalar(select(Action).where(Action.case_id == "pay_3")) is None
        assert db.get(Case, "pay_3").status == CaseStatus.ESCALATED
