import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.config import get_settings
from app.recovery_policy import fallback_action

ALLOWED_ACTIONS = frozenset({"smart_retry", "retry_later", "request_customer_action", "alternative_payment_method", "promise_to_pay", "human_review", "close_lost", "no_action"})
ALLOWED_RETRY_TYPES = frozenset({"server_side_retry", "gateway_retry", "bank_side_retry", "customer_side_retry", "instrument_retry", "not_applicable"})
INSTRUMENT_REASONS = frozenset({"card_expired", "debit_instrument_blocked", "debit_instrument_inactive"})
CUSTOMER_REASONS = frozenset({"incorrect_cvv", "incorrect_otp", "invalid_vpa", "incorrect_card_details", "incorrect_card_expiry_date", "incorrect_cardholder_name", "card_not_enrolled", "card_disabled_for_online_payments"})

RECOVERY_STRATEGY_SYSTEM_PROMPT = """You are Revive's Recovery Decision Advisor. Analyze one failed payment and recommend exactly one safest next recovery strategy. You are advisory only. Never mutate databases, change case status, increment retries, create promises or human-review records, send messages, call Razorpay/payment APIs, invent history or errors, or override deterministic application policy.

Use only supplied fields and the trusted revive_razorpay_failure_catalogue. Prioritize error_reason, error_code, error_description, error_source, error_step, payment_method, product, retry_count, case_status, opt-out state, and supplied histories. Missing values are unknown; never infer them.

Apply this hierarchy: identify the failure category; check hard policy; prefer safe bounded recovery; request customer correction when required; use an alternative payment method when the instrument is unusable; use Promise to Pay selectively for potentially recoverable lower-value cases; use human_review only as a last resort; close_lost only when recovery must stop.

Hard policy is authoritative: amount > INR 100000 requires human_review; customer_opted_out requires close_lost; retry_count >= 3 forbids another retry; an active pending promise requires waiting. The application may override your recommendation. Do not claim an overridden recommendation was executed.

Temporary failures such as payment_timed_out, request_timed_out, gateway_technical_error, bank_technical_error, PSP unavailable, payment_failed, and payment_declined generally favor smart_retry or retry_later. Customer issues such as incorrect_cvv, incorrect_otp, invalid_vpa, and incorrect card details favor request_customer_action. Expired, blocked, or inactive instruments favor request_customer_action or alternative_payment_method. Risk/security failures may require human_review. Do not choose human_review merely because a payment failed once, is unfamiliar, temporarily unavailable, or moderately uncertain.

Use retry_type only when retry is recommended: server_side_retry for general server/request failures; gateway_retry for gateway failures/timeouts; bank_side_retry for issuer/issuer_bank technical or authorization failures; customer_side_retry when customer correction is required; instrument_retry for instrument-specific failures; not_applicable otherwise.

Promise to Pay is not the default. Recommend it only for a potentially recoverable case with amount <= INR 100000, no opt-out, no pending promise, no mandatory human review, and where immediate retry is not clearly best. The application creates the promise and decides allocation.

recovery_probability estimates eventual recovery from 0.0 to 1.0; it is not a guarantee or Razorpay statistic. confidence measures how strongly the supplied evidence supports the recommendation. Both are independent values from 0.0 to 1.0. Reasoning must be concise business reasoning, explain the failure and action fit, contain no hidden chain-of-thought, and be at most 500 characters.

Return only valid JSON with exactly these fields and no markdown or additional fields: recommended_action, retry_type, recovery_probability, confidence, reasoning. recommended_action must be one of smart_retry, retry_later, request_customer_action, alternative_payment_method, promise_to_pay, human_review, close_lost, no_action. retry_type must be one of server_side_retry, gateway_retry, bank_side_retry, customer_side_retry, instrument_retry, not_applicable. The application maps these logical actions to existing action types: smart_retry/retry_later/no_action -> no_action; request_customer_action/promise_to_pay -> send_reminder; alternative_payment_method -> regenerate_link; human_review -> escalate_to_human; close_lost -> no_action."""


@dataclass(frozen=True)
class RecoveryRecommendation:
    recommended_action: str
    retry_type: str
    recovery_probability: Decimal
    confidence: Decimal
    reasoning: str
    used_fallback: bool = False


class RecoveryStrategist(Protocol):
    def recommend(self, context: dict[str, Any]) -> RecoveryRecommendation:
        ...


def build_case_context(case: Any, previous_actions: list[dict[str, Any]] | None = None, promise_history: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "case_id": case.id,
        "payment_method": case.payment_method,
        "amount": str(case.amount),
        "currency": "INR",
        "product": case.product,
        "error_code": case.raw_error_code,
        "error_reason": case.root_cause,
        "error_description": case.error_description,
        "error_source": case.error_source,
        "error_step": case.error_step,
        "retry_count": case.retry_count,
        "case_status": str(case.status),
        "customer_opted_out": case.customer_opted_out,
        "previous_actions": previous_actions or [],
        "promise_history": promise_history or [],
    }


def retry_type_for(context: dict[str, Any]) -> str:
    reason = str(context.get("error_reason") or "")
    source = str(context.get("error_source") or "")
    if reason in CUSTOMER_REASONS:
        return "customer_side_retry"
    if reason in INSTRUMENT_REASONS:
        return "instrument_retry"
    if source == "gateway" or reason in {"payment_timed_out", "request_timed_out", "gateway_technical_error"}:
        return "gateway_retry"
    if source in {"issuer", "issuer_bank"} or reason == "bank_technical_error":
        return "bank_side_retry"
    return "not_applicable"


def deterministic_fallback(context: dict[str, Any]) -> RecoveryRecommendation:
    reason = str(context.get("error_reason") or "payment_failed")
    if bool(context.get("customer_opted_out")):
        return RecoveryRecommendation("close_lost", "not_applicable", Decimal("0"), Decimal("1"), "The customer opted out, so automated recovery must stop.", True)
    if Decimal(str(context.get("amount") or "0")) > Decimal("100000"):
        return RecoveryRecommendation("human_review", "not_applicable", Decimal("0"), Decimal("1"), "The case exceeds the high-value threshold and requires human review.", True)
    if int(context.get("retry_count") or 0) >= 3:
        return RecoveryRecommendation("close_lost", "not_applicable", Decimal("0"), Decimal("1"), "The maximum number of automated retries has been exhausted.", True)
    action = fallback_action(context)
    source = str(context.get("error_source") or "unknown")
    if action == "smart_retry":
        retry_type = retry_type_for(context)
        return RecoveryRecommendation("smart_retry", retry_type if retry_type != "not_applicable" else "server_side_retry", Decimal("0.70"), Decimal("1"), f"The {reason} failure from {source} is potentially temporary, so a bounded retry is the safest fallback.", True)
    if action == "alternative_payment_method":
        return RecoveryRecommendation(action, "instrument_retry", Decimal("0.30"), Decimal("1"), f"The {reason} instrument failure makes retrying the same instrument unsafe; use an alternative payment route.", True)
    if action == "request_customer_action":
        return RecoveryRecommendation(action, "customer_side_retry", Decimal("0.35"), Decimal("1"), f"The {reason} failure requires customer correction before another payment attempt.", True)
    reasoning = f"The {reason} failure exceeds the safe automated recovery boundary and requires controlled human review." if action == "human_review" else f"The customer opted out or retries are exhausted for {reason}, so recovery must stop."
    return RecoveryRecommendation(action, "not_applicable", Decimal("0"), Decimal("1"), reasoning, True)


class GeminiRecoveryStrategy:
    def __init__(self, api_key: str | None = None, model: str | None = None, timeout_seconds: int | None = None):
        settings = get_settings()
        self.api_key = api_key or settings.gemini_api_key
        self.model = model or settings.gemini_model
        self.timeout_seconds = timeout_seconds or settings.gemini_timeout_seconds
        if not self.api_key:
            raise ValueError("GEMINI_API_KEY is not configured")

    def recommend(self, context: dict[str, Any]) -> RecoveryRecommendation:
        request_body = {"systemInstruction": {"parts": [{"text": RECOVERY_STRATEGY_SYSTEM_PROMPT}]}, "contents": [{"role": "user", "parts": [{"text": json.dumps(context, separators=(",", ":"))}]}], "generationConfig": {"responseMimeType": "application/json", "temperature": 0.1}}
        request = Request(f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={self.api_key}", data=json.dumps(request_body).encode("utf-8"), method="POST", headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                response_json = json.loads(response.read().decode("utf-8"))
            text = response_json["candidates"][0]["content"]["parts"][0]["text"]
            result = json.loads(text)
            return RecoveryRecommendation(str(result["recommended_action"]), str(result.get("retry_type", "not_applicable")), Decimal(str(result["recovery_probability"])), Decimal(str(result["confidence"])), str(result["reasoning"]))
        except (HTTPError, URLError, TimeoutError, KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("gemini_decision_failed") from exc


class SafeRecoveryStrategy:
    def __init__(self, gemini: GeminiRecoveryStrategy | None = None):
        self.gemini = gemini

    def recommend(self, context: dict[str, Any]) -> RecoveryRecommendation:
        if self.gemini:
            try:
                return validate_recommendation(self.gemini.recommend(context))
            except (TypeError, ValueError):
                return deterministic_fallback(context)
        return deterministic_fallback(context)


def build_recovery_strategy() -> RecoveryStrategist:
    settings = get_settings()
    if settings.gemini_api_key and settings.gemini_api_key != "replace-me":
        return SafeRecoveryStrategy(GeminiRecoveryStrategy())
    return SafeRecoveryStrategy()


def validate_recommendation(recommendation: Any) -> RecoveryRecommendation:
    if not isinstance(recommendation, RecoveryRecommendation):
        retry_type = getattr(recommendation, "retry_type", "not_applicable")
        recommendation = RecoveryRecommendation(recommendation.recommended_action, retry_type, Decimal(str(recommendation.recovery_probability)), Decimal(str(recommendation.confidence)), recommendation.reasoning, bool(getattr(recommendation, "used_fallback", False)))
    if recommendation.recommended_action not in ALLOWED_ACTIONS:
        raise ValueError("unsupported_recommended_action")
    if recommendation.retry_type not in ALLOWED_RETRY_TYPES:
        raise ValueError("unsupported_retry_type")
    if not 0 <= recommendation.recovery_probability <= 1:
        raise ValueError("invalid_recovery_probability")
    if not 0 <= recommendation.confidence <= 1:
        raise ValueError("invalid_confidence")
    if not recommendation.reasoning.strip() or len(recommendation.reasoning) > 500:
        raise ValueError("invalid_reasoning")
    return recommendation
