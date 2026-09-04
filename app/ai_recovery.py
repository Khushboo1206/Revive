from dataclasses import dataclass
from decimal import Decimal
import json
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.config import get_settings

ALLOWED_ACTIONS = frozenset({"smart_retry", "retry_later", "request_customer_action", "alternative_payment_method", "promise_to_pay", "human_review", "close_lost", "no_action"})
GEMINI_SYSTEM_PROMPT = """You are Revive's recovery strategist. Recommend exactly one allowed recovery action for the supplied payment case. You are advisory only: never mutate state, send messages, call payment APIs, or override business rules. Return only valid JSON with recommended_action, recovery_probability, confidence, and concise reasoning. Allowed actions: smart_retry, retry_later, request_customer_action, alternative_payment_method, promise_to_pay, human_review, close_lost, no_action."""


@dataclass(frozen=True)
class RecoveryRecommendation:
    recommended_action: str
    recovery_probability: Decimal
    confidence: Decimal
    reasoning: str


class RecoveryStrategist(Protocol):
    def recommend(self, context: dict[str, Any]) -> RecoveryRecommendation:
        ...


class RuleBasedRecoveryStrategist:
    """Local test strategist; replaceable with a structured LLM adapter later."""

    def recommend(self, context: dict[str, Any]) -> RecoveryRecommendation:
        reason = str(context.get("root_cause") or context.get("error_reason") or "payment failure")
        if any(term in reason for term in ("expired", "blocked", "inactive", "invalid")):
            return RecoveryRecommendation("request_customer_action", Decimal("0.35"), Decimal("0.80"), f"Customer action is required for {reason}.")
        if any(term in reason for term in ("fraud", "risk", "authentication")):
            return RecoveryRecommendation("human_review", Decimal("0.25"), Decimal("0.70"), f"The {reason} failure needs controlled review.")
        return RecoveryRecommendation("smart_retry", Decimal("0.70"), Decimal("0.80"), f"The {reason} failure may be temporary and is eligible for a bounded retry.")


class GeminiRecoveryStrategist:
    def __init__(self, api_key: str | None = None, model: str | None = None, timeout_seconds: int | None = None):
        settings = get_settings()
        self.api_key = api_key or settings.gemini_api_key
        self.model = model or settings.gemini_model
        self.timeout_seconds = timeout_seconds or settings.gemini_timeout_seconds
        if not self.api_key:
            raise ValueError("GEMINI_API_KEY is not configured")

    def recommend(self, context: dict[str, Any]) -> RecoveryRecommendation:
        request_body = {
            "systemInstruction": {"parts": [{"text": GEMINI_SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": json.dumps(context, separators=(",", ":"))}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.1},
        }
        request = Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={self.api_key}",
            data=json.dumps(request_body).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                response_json = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise ValueError("gemini_request_failed") from exc
        try:
            text = response_json["candidates"][0]["content"]["parts"][0]["text"]
            result = json.loads(text)
            return RecoveryRecommendation(str(result["recommended_action"]), Decimal(str(result["recovery_probability"])), Decimal(str(result["confidence"])), str(result["reasoning"]))
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("gemini_response_invalid") from exc


def build_recovery_strategist() -> RecoveryStrategist:
    settings = get_settings()
    return GeminiRecoveryStrategist() if settings.gemini_api_key else RuleBasedRecoveryStrategist()


def validate_recommendation(recommendation: RecoveryRecommendation) -> RecoveryRecommendation:
    if recommendation.recommended_action not in ALLOWED_ACTIONS:
        raise ValueError("unsupported_recommended_action")
    if not 0 <= recommendation.recovery_probability <= 1:
        raise ValueError("invalid_recovery_probability")
    if not 0 <= recommendation.confidence <= 1:
        raise ValueError("invalid_confidence")
    if not recommendation.reasoning.strip() or len(recommendation.reasoning) > 500:
        raise ValueError("invalid_reasoning")
    return recommendation
