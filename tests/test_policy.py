from decimal import Decimal

from app.policy import RecoveryPolicy


def test_policy_approves_recoverable_failure() -> None:
    decision = RecoveryPolicy().decide(Decimal("0.70"), 0, Decimal("100"))
    assert decision.approved
    assert decision.strategy == "mandate_retry"


def test_policy_stops_after_retry_limit() -> None:
    decision = RecoveryPolicy().decide(Decimal("0.90"), 3, Decimal("100"))
    assert not decision.approved
    assert decision.reason == "retry_limit_reached"
