from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class RecoveryDecision:
    approved: bool
    strategy: str
    reason: str


class RecoveryPolicy:
    def __init__(self, minimum_probability: Decimal = Decimal("0.35"), max_retries: int = 3):
        self.minimum_probability = minimum_probability
        self.max_retries = max_retries

    def decide(self, probability: Decimal, retry_count: int, amount: Decimal) -> RecoveryDecision:
        if retry_count >= self.max_retries:
            return RecoveryDecision(False, "stop", "retry_limit_reached")
        if amount <= 0:
            return RecoveryDecision(False, "stop", "non_positive_amount")
        if probability < self.minimum_probability:
            return RecoveryDecision(False, "stop", "recovery_probability_below_threshold")
        return RecoveryDecision(True, "mandate_retry", "policy_approved")
