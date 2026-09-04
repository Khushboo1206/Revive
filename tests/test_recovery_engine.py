from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.ai_recovery import RecoveryRecommendation
from app.db import Base
from app.models import Action, AuditLog, Case, CaseStatus, HumanReviewQueue, Promise, PromiseStatus
from app.recovery_engine import RecoveryEngine


class FixedStrategist:
    def __init__(self, action: str = "smart_retry", confidence: str = "0.90"):
        self.action = action
        self.confidence = Decimal(confidence)

    def recommend(self, context: dict) -> RecoveryRecommendation:
        return RecoveryRecommendation(self.action, Decimal("0.80"), self.confidence, "Test recommendation for the current payment context.")


def make_db() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return Session(engine)


def add_case(db: Session, case_id: str = "pay_test", amount: str = "5000") -> Case:
    case = Case(id=case_id, amount=Decimal(amount), source_type="checkout_dropoff", payment_method="card", root_cause="network_or_gateway_timeout", raw_error_code="GATEWAY_ERROR", status=CaseStatus.OPEN)
    db.add(case)
    db.commit()
    return case


def test_retry_lifecycle_and_exhaustion() -> None:
    db = make_db()
    case = add_case(db)
    engine = RecoveryEngine(FixedStrategist(), retry_outcome=lambda _: "retry_again")
    engine.run_cycle(db, datetime.now(timezone.utc))
    assert case.status == CaseStatus.RETRY_1
    assert case.last_retry_at is not None
    assert case.next_retry_at is not None
    case.next_action_at = None
    db.commit()
    engine.run_cycle(db, datetime.now(timezone.utc))
    assert case.status == CaseStatus.RETRY_2
    case.next_action_at = None
    db.commit()
    engine.run_cycle(db, datetime.now(timezone.utc))
    assert case.status == CaseStatus.RETRY_3
    case.next_action_at = None
    db.commit()
    engine.run_cycle(db, datetime.now(timezone.utc))
    assert case.status == CaseStatus.CLOSED_LOST
    assert db.scalar(select(func.count(Action.id)).where(Action.case_id == case.id)) == 3


def test_high_value_case_always_escalates_without_duplicate_review() -> None:
    db = make_db()
    case = add_case(db, amount="100001")
    engine = RecoveryEngine(FixedStrategist("smart_retry"))
    engine.run_cycle(db)
    engine.run_cycle(db)
    assert case.status == CaseStatus.ESCALATED
    assert db.scalar(select(func.count(HumanReviewQueue.id)).where(HumanReviewQueue.case_id == case.id)) == 1
    assert db.scalar(select(func.count(Action.id)).where(Action.case_id == case.id)) == 1


def test_low_confidence_recommendation_routes_to_human_review() -> None:
    db = make_db()
    case = add_case(db)
    RecoveryEngine(FixedStrategist("smart_retry", "0.10")).run_cycle(db)
    action = db.scalar(select(Action).where(Action.case_id == case.id))
    assert case.status == CaseStatus.ESCALATED
    assert action.action_type == "escalate_to_human"


def test_promise_to_pay_is_bounded_and_not_duplicated() -> None:
    db = make_db()
    case = add_case(db, amount="25000")
    engine = RecoveryEngine(FixedStrategist("promise_to_pay"))
    engine.run_cycle(db)
    promise = db.scalar(select(Promise).where(Promise.case_id == case.id))
    assert promise is not None
    assert case.status == CaseStatus.ESCALATED
    assert timedelta(minutes=1) <= promise.promised_date - promise.created_at <= timedelta(minutes=5, seconds=1)
    engine.run_cycle(db)
    assert db.scalar(select(func.count(Promise.id)).where(Promise.case_id == case.id, Promise.status == PromiseStatus.PENDING)) == 1


def test_due_promise_breaks_and_resumes_recovery() -> None:
    db = make_db()
    case = add_case(db)
    case.status = CaseStatus.ESCALATED
    now = datetime.now(timezone.utc)
    promise = Promise(case_id=case.id, promised_date=now - timedelta(seconds=1), status=PromiseStatus.PENDING)
    db.add(promise)
    db.commit()
    RecoveryEngine(FixedStrategist(), retry_outcome=lambda _: "recovered").run_cycle(db, now)
    assert promise.status == PromiseStatus.BROKEN
    assert case.status == CaseStatus.RECOVERED
    assert db.scalar(select(AuditLog).where(AuditLog.case_id == case.id, AuditLog.event.contains("promise_broken"))) is not None


def test_recovered_case_is_not_processed_again() -> None:
    db = make_db()
    case = add_case(db)
    case.status = CaseStatus.RECOVERED
    db.commit()
    assert RecoveryEngine(FixedStrategist()).run_cycle(db) == 0
    assert db.scalar(select(Action).where(Action.case_id == case.id)) is None


def test_customer_opt_out_closes_case_without_retry() -> None:
    db = make_db()
    case = add_case(db)
    case.customer_opted_out = True
    db.commit()
    RecoveryEngine(FixedStrategist(), retry_outcome=lambda _: "recovered").run_cycle(db)
    assert case.status == CaseStatus.CLOSED_LOST
    assert case.retry_count == 0
    assert db.scalar(select(Action).where(Action.case_id == case.id)) is None
    audit = db.scalar(select(AuditLog).where(AuditLog.case_id == case.id, AuditLog.event.contains("customer_opted_out")))
    assert audit is not None


def test_retry_action_describes_implemented_work() -> None:
    db = make_db()
    case = add_case(db)
    RecoveryEngine(FixedStrategist(), retry_outcome=lambda _: "retry_again").run_cycle(db)
    action = db.scalar(select(Action).where(Action.case_id == case.id))
    assert action.agent_reasoning.startswith("Implemented retry #1")
