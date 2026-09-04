from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db import Base
from app.main import _process_webhook_payload
from app.models import WebhookEvent
from app.test_event_generator import SimulationError, generate_event, get_scenario


def test_specific_card_scenario_generates_realistic_unique_event() -> None:
    scenario = get_scenario("CARD-002")
    first, first_metadata = generate_event(scenario, "card", "checkout", 100, 1000)
    second, second_metadata = generate_event(scenario, "card", "checkout", 100, 1000)
    first_payment = first["payload"]["payment"]["entity"]
    second_payment = second["payload"]["payment"]["entity"]
    assert first_metadata["scenario_id"] == "CARD-002"
    assert first_payment["error_reason"] == "insufficient_funds"
    assert first_payment["card"]["id"] != second_payment["card"]["id"]
    assert first_payment["id"] != second_payment["id"]
    assert first_payment["amount"] <= 100000


def test_random_method_and_product_are_catalogue_compatible() -> None:
    scenario = get_scenario("GEN-001")
    event, metadata = generate_event(scenario, "upi", "payment_page")
    payment = event["payload"]["payment"]["entity"]
    assert metadata["payment_method"] == "upi"
    assert metadata["product"] == "payment_page"
    assert payment["vpa"] == "test@upi"
    assert payment["card"] is None


def test_incompatible_method_is_rejected() -> None:
    try:
        generate_event(get_scenario("CARD-001"), "wallet", "checkout")
    except SimulationError as exc:
        assert "does not support" in str(exc)
    else:
        raise AssertionError("incompatible payment method was accepted")


def test_simulated_event_metadata_is_persisted() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    event, metadata = generate_event(get_scenario("CARD-001"), "card", "checkout")
    with Session(engine) as db:
        result = _process_webhook_payload(db, event, metadata)
        saved = db.scalar(select(WebhookEvent).where(WebhookEvent.razorpay_event_id == event["id"]))
        assert result.risk_id == event["payload"]["payment"]["entity"]["id"]
        assert saved is not None
        assert saved.is_simulated is True
        assert saved.scenario_id == "CARD-001"
        assert saved.source == "simulator"


def test_catalogue_error_reason_is_preserved_on_case() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    event, metadata = generate_event(get_scenario("CARD-003"), "card", "checkout")
    with Session(engine) as db:
        _process_webhook_payload(db, event, metadata)
        from app.models import Case

        case = db.get(Case, event["payload"]["payment"]["entity"]["id"])
        assert case.root_cause == "card_expired"


def test_simulator_can_generate_opt_out_case(monkeypatch) -> None:
    monkeypatch.setattr("app.test_event_generator.random.random", lambda: 0.01)
    event, metadata = generate_event(get_scenario("GEN-002"), "card", "checkout")
    assert metadata["customer_opted_out"] is True
    assert event["payload"]["payment"]["entity"]["notes"]["customer_opted_out"] is True