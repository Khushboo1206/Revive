import random
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.models import Case, CaseStatus, Promise

HIGH_VALUE_LIMIT = Decimal("100000")
MAX_RETRIES = 3
TEMPORARY_REASONS = frozenset({"payment_timed_out", "request_timed_out", "bank_technical_error", "gateway_technical_error", "psp_app_not_available", "psp_not_available", "payment_failed", "payment_declined"})
RISK_REASONS = frozenset({"payment_risk_check_failed", "fraud_rejection", "verification_failed"})
INSTRUMENT_REASONS = frozenset({"card_expired", "debit_instrument_blocked", "debit_instrument_inactive"})
CUSTOMER_REASONS = frozenset({"incorrect_cvv", "incorrect_otp", "invalid_vpa", "incorrect_card_details", "incorrect_card_expiry_date", "incorrect_cardholder_name", "card_not_enrolled", "card_disabled_for_online_payments"})
BALANCE_REASONS = frozenset({"insufficient_funds", "transaction_limit_exceeded", "transaction_daily_limit_exceeded", "transaction_frequency_limit_exceeded"})


@dataclass(frozen=True)
class HardPolicyDecision:
    action: str | None
    reason: str | None = None


def hard_policy(case: Case, pending_promise: Promise | None) -> HardPolicyDecision:
    if case.status in {CaseStatus.RECOVERED, CaseStatus.CLOSED_LOST, CaseStatus.ESCALATED}:
        return HardPolicyDecision(None)
    if case.customer_opted_out:
        return HardPolicyDecision("close_lost", "customer_opted_out")
    if case.amount > HIGH_VALUE_LIMIT:
        return HardPolicyDecision("human_review", "high_value_threshold")
    if pending_promise:
        return HardPolicyDecision("wait", "active_promise")
    if case.retry_count >= MAX_RETRIES:
        return HardPolicyDecision("close_lost", "retries_exhausted")
    return HardPolicyDecision(None)


def is_temporary(reason: str | None) -> bool:
    return reason in TEMPORARY_REASONS or "timeout" in str(reason or "") or "technical" in str(reason or "")


def fallback_action(case_context: dict[str, Any]) -> str:
    reason = str(case_context.get("error_reason") or "")
    if case_context.get("customer_opted_out"):
        return "close_lost"
    if Decimal(str(case_context.get("amount") or "0")) > HIGH_VALUE_LIMIT:
        return "human_review"
    if int(case_context.get("retry_count") or 0) >= MAX_RETRIES:
        return "close_lost"
    if reason in RISK_REASONS:
        return "human_review"
    if reason in INSTRUMENT_REASONS:
        return "alternative_payment_method"
    if reason in CUSTOMER_REASONS or reason in BALANCE_REASONS:
        return "request_customer_action"
    return "smart_retry"


def allocate_low_value_escalation(promise_weight: float = 0.40) -> str:
    return "promise_to_pay" if random.random() < promise_weight else "human_review"
