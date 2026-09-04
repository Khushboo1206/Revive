from decimal import Decimal
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ActionType, AuditLog, Case, CaseStatus, HumanReviewQueue, Promise, PromiseStatus, ReviewReason
from app.policy import RecoveryPolicy
from app.schemas import RecoveryResult


def classify_payment_failure(payment: dict[str, Any]) -> str:
    method = str(payment.get("method") or "").lower()
    explicit_reason = str(payment.get("error_reason") or "").strip().lower()
    if explicit_reason:
        return explicit_reason
    error_text = " ".join(str(payment.get(field) or "") for field in ("error_code", "error_description", "error_reason", "error_step", "error_source")).lower()
    details = f"{method} {error_text}"
    patterns = (
        ("wallet_insufficient_balance", ("insufficient", "low balance"), "wallet"),
        ("card_expired", ("expired", "expiry"), "card"),
        ("card_details_invalid", ("invalid card", "invalid cvv", "incorrect cvv"), "card"),
        ("bank_declined", ("bank declined", "issuer declined", "declined by bank"), ""),
        ("transaction_limit_reached", ("limit exceeded", "daily limit", "transaction limit"), ""),
        ("upi_pin_failed", ("upi pin", "incorrect pin", "wrong pin"), "upi"),
        ("upi_timeout", ("upi", "timed out", "timeout"), ""),
        ("authentication_failed", ("authentication", "otp", "3d secure", "3ds"), ""),
        ("network_or_gateway_timeout", ("gateway timeout", "network timeout", "timed out", "timeout"), ""),
        ("fraud_rejection", ("fraud", "risk check", "risk rejection"), ""),
        ("currency_or_international_restriction", ("currency", "international", "not supported"), ""),
        ("invalid_payment_details", ("invalid", "bad request", "missing"), ""),
    )
    for root_cause, keywords, required_method in patterns:
        if required_method and required_method not in method:
            continue
        if any(keyword in details for keyword in keywords):
            return root_cause
    return "payment_failed"


def process_payment_failed(db: Session, payload: dict[str, Any], policy: RecoveryPolicy | None = None) -> RecoveryResult:
    event_id = str(payload.get("id", ""))
    payment = payload.get("payload", {}).get("payment", {}).get("entity", {})
    payment_id = str(payment.get("id", ""))
    notes = payment.get("notes") if isinstance(payment.get("notes"), dict) else {}
    product = notes.get("product")
    customer_opted_out = bool(notes.get("customer_opted_out", False))
    case_id = payment_id or str(uuid4())
    existing = db.get(Case, case_id)
    if existing:
        duplicate = db.scalar(select(AuditLog).where(AuditLog.case_id == case_id, AuditLog.event.contains(f"event_id={event_id}")))
        existing.payment_method = existing.payment_method or payment.get("method")
        existing.order_id = existing.order_id or payment.get("order_id")
        existing.error_description = existing.error_description or payment.get("error_description")
        existing.error_source = existing.error_source or payment.get("error_source")
        existing.error_step = existing.error_step or payment.get("error_step")
        if not duplicate:
            existing.next_action_at = datetime.now(timezone.utc)
            db.add(AuditLog(case_id=case_id, event=f"Payment failure queued: event_id={event_id}, retry_count={existing.retry_count}"))
        db.commit()
        return RecoveryResult(risk_id=existing.id, event_id=event_id, status=existing.status, approved=False, strategy="pending", reason="duplicate_event_ignored" if duplicate else "queued_for_recovery_engine", recovery_probability=Decimal("0.00"))

    amount = Decimal(payment.get("amount", 0)) / Decimal(100)
    retry_time = datetime.now(timezone.utc)
    case = Case(id=case_id, customer_id=payment.get("customer_id"), amount=amount, source_type="subscription_failed" if product == "subscription" else "checkout_dropoff", raw_error_code=payment.get("error_code"), root_cause=classify_payment_failure(payment), payment_method=payment.get("method"), order_id=payment.get("order_id"), error_description=payment.get("error_description"), error_source=payment.get("error_source"), error_step=payment.get("error_step"), product=product, customer_opted_out=customer_opted_out, retry_count=0, last_retry_at=None, next_retry_at=None, next_action_at=retry_time, status=CaseStatus.OPEN)
    db.add(case)
    db.flush()
    db.add(AuditLog(case_id=case_id, event=f"Payment failed: event_id={event_id}, root_cause={case.root_cause}"))
    db.commit()
    return RecoveryResult(risk_id=case_id, event_id=event_id, status=case.status, approved=False, strategy="pending", reason="queued_for_recovery_engine", recovery_probability=Decimal("0.00"))


def process_payment_captured(db: Session, payload: dict[str, Any]) -> RecoveryResult | None:
    event_id = str(payload.get("id", ""))
    payment = payload.get("payload", {}).get("payment", {}).get("entity", {})
    payment_id = str(payment.get("id", ""))
    case = db.get(Case, payment_id)
    if not case:
        return None
    if case.status == CaseStatus.RECOVERED:
        return RecoveryResult(risk_id=case.id, event_id=event_id, status=case.status, approved=True, strategy="none", reason="duplicate_event_ignored", recovery_probability=Decimal("1.00"))
    case.status = CaseStatus.RECOVERED
    db.add(AuditLog(case_id=case.id, event=f"Payment captured: event_id={event_id}"))
    db.commit()
    return RecoveryResult(risk_id=case.id, event_id=event_id, status=case.status, approved=True, strategy="none", reason="payment_recovered", recovery_probability=Decimal("1.00"))


def process_payment_link_event(db: Session, payload: dict[str, Any]) -> RecoveryResult | None:
    event_id = str(payload.get("id", ""))
    event = str(payload.get("event", ""))
    link = payload.get("payload", {}).get("payment_link", {}).get("entity", {})
    link_id = str(link.get("id", ""))
    if not link_id:
        return None
    case = db.get(Case, link_id)
    if not case:
        amount = Decimal(link.get("amount", 0)) / Decimal(100)
        case = Case(id=link_id, customer_id=link.get("customer_id"), amount=amount, source_type="checkout_dropoff", raw_error_code=event, root_cause=event.replace("payment_link.", ""), status=CaseStatus.OPEN)
        db.add(case)
        db.flush()

    if event == "payment_link.paid":
        case.status = CaseStatus.RECOVERED
        action_type = ActionType.NO_ACTION
        reason = "payment_link_paid"
        approved = True
        probability = Decimal("1.00")
    elif event == "payment_link.partially_paid":
        case.status = CaseStatus.OPEN
        action_type = ActionType.SEND_REMINDER
        reason = "payment_link_partially_paid"
        approved = True
        probability = Decimal("0.50")
    else:
        case.status = CaseStatus.CLOSED_LOST
        action_type = ActionType.NO_ACTION
        reason = f"{event.replace('payment_link.', '')}_payment_link"
        approved = False
        probability = Decimal("0.00")
    db.add(AuditLog(case_id=case.id, event=f"Payment link event: event_id={event_id}, event={event}"))
    db.commit()
    return RecoveryResult(risk_id=case.id, event_id=event_id, status=case.status, approved=approved, strategy=action_type.value, reason=reason, recovery_probability=probability)


def record_promise(db: Session, case_id: str, promised_date: datetime) -> Promise:
    if not db.get(Case, case_id):
        raise ValueError("case_not_found")
    promise = Promise(case_id=case_id, promised_date=promised_date, status=PromiseStatus.PENDING)
    db.add(promise)
    db.flush()
    db.add(AuditLog(case_id=case_id, event=f"Payment promise created: promise_id={promise.id}"))
    db.commit()
    return promise


def update_promise_status(db: Session, promise_id: int, promise_status: PromiseStatus) -> Promise:
    promise = db.get(Promise, promise_id)
    if not promise:
        raise ValueError("promise_not_found")
    promise.status = promise_status
    db.add(AuditLog(case_id=promise.case_id, event=f"Payment promise updated: promise_id={promise.id}, status={promise_status.value}"))
    if promise_status == PromiseStatus.BROKEN:
        escalate_case(db, promise.case_id, ReviewReason.BROKEN_PROMISE)
    else:
        db.commit()
    return promise


def escalate_case(db: Session, case_id: str, reason: ReviewReason) -> HumanReviewQueue:
    case = db.get(Case, case_id)
    if not case:
        raise ValueError("case_not_found")
    queue_entry = db.scalar(select(HumanReviewQueue).where(HumanReviewQueue.case_id == case_id, HumanReviewQueue.reviewed.is_(False)))
    if queue_entry:
        return queue_entry
    case.status = CaseStatus.ESCALATED
    queue_entry = HumanReviewQueue(case_id=case_id, reason=reason)
    db.add(queue_entry)
    db.add(AuditLog(case_id=case_id, event=f"Case escalated: reason={reason.value}"))
    db.commit()
    return queue_entry
