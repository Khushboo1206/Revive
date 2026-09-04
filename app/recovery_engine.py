import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.recovery_policy import allocate_low_value_escalation, hard_policy
from app.recovery_strategy import RecoveryRecommendation, RecoveryStrategist, build_case_context, build_recovery_strategy, deterministic_fallback, validate_recommendation
from app.config import get_settings
from app.models import Action, ActionType, AuditLog, Case, CaseStatus, HumanReviewQueue, Promise, PromiseStatus, ReviewReason


class RecoveryEngine:
    def __init__(self, strategist: RecoveryStrategist | None = None, retry_outcome: Callable[[Case], str] | None = None):
        self.strategist = strategist or build_recovery_strategy()
        self.retry_outcome = retry_outcome or (lambda case: "recovered" if random.random() < 0.25 else "retry_again")

    def run_cycle(self, db: Session, now: datetime | None = None) -> int:
        now = now or datetime.now(timezone.utc)
        processed = self._process_promises(db, now)
        eligible = db.scalars(select(Case).where(Case.status.not_in([CaseStatus.RECOVERED, CaseStatus.CLOSED_LOST, CaseStatus.ESCALATED]), (Case.next_action_at.is_(None)) | (Case.next_action_at <= now))).all()
        for case in eligible:
            if self._evaluate_case(db, case, now):
                processed += 1
        return processed

    def _audit(self, db: Session, case: Case, event: str) -> None:
        actor = "" if "actor=" in event else "; actor=recovery_engine"
        db.add(AuditLog(case_id=case.id, event=f"{event}{actor}"))

    def _process_promises(self, db: Session, now: datetime) -> int:
        count = 0
        promises = db.scalars(select(Promise).where(Promise.status == PromiseStatus.PENDING, Promise.promised_date <= now)).all()
        for promise in promises:
            case = db.get(Case, promise.case_id)
            if not case:
                continue
            self._audit(db, case, f"promise_due; promise_id={promise.id}; actor=recovery_engine")
            if case.status == CaseStatus.RECOVERED:
                promise.status = PromiseStatus.KEPT
                self._audit(db, case, f"promise_kept; promise_id={promise.id}; old_state=pending; new_state=kept")
            else:
                promise.status = PromiseStatus.BROKEN
                case.status = CaseStatus.OPEN
                case.next_action_at = now
                self._audit(db, case, f"promise_broken; promise_id={promise.id}; old_state=pending; new_state=broken")
            count += 1
        db.commit()
        return count

    def _evaluate_case(self, db: Session, case: Case, now: datetime) -> bool:
        self._audit(db, case, f"case_evaluated; status={case.status}; retry_count={case.retry_count}")
        pending = db.scalar(select(Promise).where(Promise.case_id == case.id, Promise.status == PromiseStatus.PENDING))
        policy_decision = hard_policy(case, pending)
        if policy_decision.action == "close_lost":
            return self._close(db, case, policy_decision.reason or "policy_closed")
        if policy_decision.action == "human_review":
            return self._escalate(db, case, policy_decision.reason or "policy_escalation")
        if policy_decision.action == "wait":
            case.next_action_at = pending.promised_date
            db.commit()
            return False
        previous_actions = [{"action_type": action.action_type, "reasoning": action.agent_reasoning} for action in db.scalars(select(Action).where(Action.case_id == case.id).order_by(Action.created_at.desc()).limit(10))]
        promise_history = [{"status": promise.status, "promised_date": promise.promised_date.isoformat()} for promise in db.scalars(select(Promise).where(Promise.case_id == case.id).order_by(Promise.created_at.desc()).limit(10))]
        context = build_case_context(case, previous_actions, promise_history)
        try:
            recommendation = validate_recommendation(self.strategist.recommend(context))
        except (TypeError, ValueError) as exc:
            recommendation = deterministic_fallback(context)
        self._audit(db, case, f"ai_recommendation_generated; action={recommendation.recommended_action}; confidence={recommendation.confidence}; actor=ai")
        if recommendation.used_fallback:
            self._audit(db, case, "ai_decision_failed; deterministic_fallback_used=true; actor=policy_engine")
        if recommendation.confidence < Decimal(str(get_settings().ai_confidence_threshold)):
            if recommendation.recommended_action in {"smart_retry", "retry_later"} and recommendation.retry_type in {"server_side_retry", "gateway_retry", "bank_side_retry"}:
                recommendation = deterministic_fallback(context)
                self._audit(db, case, "ai_confidence_low; temporary_failure_fallback_to_retry=true; actor=policy_engine")
            else:
                return self._escalate(db, case, "ai_confidence_below_threshold", recommendation.reasoning)
        return self._execute(db, case, recommendation, now)

    def _execute(self, db: Session, case: Case, recommendation: RecoveryRecommendation, now: datetime) -> bool:
        action_map = {"smart_retry": ActionType.NO_ACTION, "retry_later": ActionType.NO_ACTION, "request_customer_action": ActionType.SEND_REMINDER, "alternative_payment_method": ActionType.REGENERATE_LINK, "promise_to_pay": ActionType.SEND_REMINDER, "human_review": ActionType.ESCALATE_TO_HUMAN, "close_lost": ActionType.NO_ACTION, "no_action": ActionType.NO_ACTION}
        action_type = action_map[recommendation.recommended_action]
        if recommendation.recommended_action in {"human_review"}:
            if case.amount <= Decimal("100000") and case.root_cause not in {"payment_risk_check_failed", "fraud_rejection", "verification_failed"}:
                allocated = allocate_low_value_escalation(get_settings().promise_to_pay_weight)
                if allocated == "promise_to_pay":
                    recommendation = RecoveryRecommendation("promise_to_pay", "not_applicable", recommendation.recovery_probability, recommendation.confidence, recommendation.reasoning)
                    self._audit(db, case, "policy_allocation; escalation_to_promise_to_pay=true; actor=policy_engine")
                    return self._execute(db, case, recommendation, now)
                else:
                    self._audit(db, case, "policy_allocation; escalation_to_human_review=true; actor=policy_engine")
            return self._escalate(db, case, recommendation.recommended_action, recommendation.reasoning)
        if recommendation.recommended_action == "close_lost":
            return self._close(db, case, recommendation.reasoning, recommendation.reasoning)
        if recommendation.recommended_action == "promise_to_pay":
            if case.amount > Decimal("100000") or case.customer_opted_out or db.scalar(select(Promise).where(Promise.case_id == case.id, Promise.status == PromiseStatus.PENDING)):
                return self._escalate(db, case, "promise_to_pay_not_permitted", recommendation.reasoning)
            case.status = CaseStatus.ESCALATED
            promise = Promise(case_id=case.id, promised_date=now + timedelta(minutes=random.randint(1, 5)), status=PromiseStatus.PENDING)
            db.add(promise)
            db.flush()
            case.next_action_at = promise.promised_date
            db.add(Action(case_id=case.id, action_type=action_type, agent_reasoning=recommendation.reasoning))
            self._audit(db, case, f"case_escalated; reason=promise_to_pay; new_state=escalated")
            self._audit(db, case, f"promise_created; promise_id={promise.id}; promised_date={promise.promised_date}")
            db.commit()
            return True
        if recommendation.recommended_action in {"smart_retry", "retry_later"}:
            case.retry_count += 1
            case.last_retry_at = now
            case.status = [CaseStatus.OPEN, CaseStatus.RETRY_1, CaseStatus.RETRY_2, CaseStatus.RETRY_3][case.retry_count]
            outcome = self.retry_outcome(case)
            case.next_action_at = now + timedelta(minutes=1) if outcome == "retry_again" else None
            case.next_retry_at = case.next_action_at
            db.add(Action(case_id=case.id, action_type=action_type, agent_reasoning=f"Implemented retry #{case.retry_count} ({recommendation.recommended_action}, {recommendation.retry_type}); {recommendation.reasoning}"))
            self._audit(db, case, f"retry_started; retry_count={case.retry_count}; new_state={case.status}")
            self._audit(db, case, f"retry_{'succeeded' if outcome == 'recovered' else 'failed'}; outcome={outcome}; retry_count={case.retry_count}")
            if outcome == "recovered":
                case.status = CaseStatus.RECOVERED
                case.next_action_at = None
                case.next_retry_at = None
                self._audit(db, case, f"case_recovered; old_state={CaseStatus.RETRY_1 if case.retry_count == 1 else CaseStatus.RETRY_2 if case.retry_count == 2 else CaseStatus.RETRY_3}; new_state=recovered; action=simulated_retry")
            else:
                self._audit(db, case, f"retry_scheduled; next_retry_at={case.next_action_at}; action=simulated_retry")
            db.commit()
            return True
        message = "Please review your payment details and try again." if recommendation.recommended_action == "request_customer_action" else None
        implemented = "customer_action_draft_created" if recommendation.recommended_action == "request_customer_action" else "alternative_payment_route_recommended" if recommendation.recommended_action == "alternative_payment_method" else recommendation.recommended_action
        db.add(Action(case_id=case.id, action_type=action_type, channel="email" if message else None, message=message, agent_reasoning=f"Implemented {implemented}; {recommendation.reasoning}"))
        case.next_action_at = now + timedelta(hours=2)
        self._audit(db, case, f"action_executed; action_type={action_type}; recommendation={recommendation.recommended_action}")
        db.commit()
        return True

    def _close(self, db: Session, case: Case, reason: str, reasoning: str | None = None) -> bool:
        case.status = CaseStatus.CLOSED_LOST
        case.next_action_at = None
        if reasoning:
            db.add(Action(case_id=case.id, action_type=ActionType.NO_ACTION, agent_reasoning=f"Implemented case closure; {reasoning}"))
        self._audit(db, case, f"case_closed_lost; reason={reason}; new_state=closed_lost")
        db.commit()
        return True

    def _escalate(self, db: Session, case: Case, reason: str, reasoning: str | None = None) -> bool:
        case.status = CaseStatus.ESCALATED
        case.next_action_at = None
        review = db.scalar(select(HumanReviewQueue).where(HumanReviewQueue.case_id == case.id, HumanReviewQueue.reviewed.is_(False)))
        if not review:
            review = HumanReviewQueue(case_id=case.id, reason=ReviewReason.CAPS_EXCEEDED)
            db.add(review)
            db.flush()
            db.add(Action(case_id=case.id, action_type=ActionType.ESCALATE_TO_HUMAN, agent_reasoning=f"Implemented human review escalation; {reasoning or reason}"))
            self._audit(db, case, f"human_review_created; reason={reason}; new_state=escalated")
        db.commit()
        return True
