import json
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.config import PROJECT_ROOT

CATALOG_PATH = PROJECT_ROOT / "revive_razorpay_failure_catalog.json"
SUPPORTED_METHODS = {"card", "upi", "netbanking", "wallet", "cardless_emi", "emandate"}
SUPPORTED_PRODUCTS = {"checkout", "payment_link", "payment_page", "subscription"}


class SimulationError(ValueError):
    pass


def load_catalogue() -> dict[str, Any]:
    with CATALOG_PATH.open(encoding="utf-8") as catalogue_file:
        return json.load(catalogue_file)


def scenarios() -> list[dict[str, Any]]:
    return load_catalogue()["scenarios"]


def get_scenario(scenario_id: str) -> dict[str, Any]:
    scenario = next((item for item in scenarios() if item["id"] == scenario_id), None)
    if scenario is None:
        raise SimulationError(f"unknown scenario_id: {scenario_id}")
    return scenario


def _choose_method(scenario: dict[str, Any], payment_method: str | None) -> str:
    requested = (payment_method or "any").lower()
    if requested != "any" and requested not in SUPPORTED_METHODS:
        raise SimulationError(f"invalid payment_method: {payment_method}")
    scenario_method = scenario["method"]
    if scenario_method == "any":
        return random.choice(sorted(SUPPORTED_METHODS)) if requested == "any" else requested
    if requested != "any" and requested != scenario_method:
        raise SimulationError(f"scenario {scenario['id']} does not support payment_method: {requested}")
    return scenario_method


def _choose_product(scenario: dict[str, Any], product: str | None) -> str:
    requested = (product or "any").lower()
    if requested != "any" and requested not in SUPPORTED_PRODUCTS:
        raise SimulationError(f"invalid product: {product}")
    products = scenario["product"]
    if requested != "any" and requested not in products:
        raise SimulationError(f"scenario {scenario['id']} does not support product: {requested}")
    return random.choice(products) if requested == "any" else requested


def _amount(min_amount: int, max_amount: int) -> int:
    if min_amount < 100 or max_amount > 100000 or min_amount > max_amount:
        raise SimulationError("amount range must be between INR 100 and INR 100,000")
    return random.randint(min_amount * 100, max_amount * 100)


def generate_event(scenario: dict[str, Any], payment_method: str | None = None, product: str | None = None, min_amount: int = 100, max_amount: int = 100000) -> tuple[dict[str, Any], dict[str, Any]]:
    method = _choose_method(scenario, payment_method)
    selected_product = _choose_product(scenario, product)
    payment_id = f"pay_TEST_{uuid4().hex[:16]}"
    order_id = f"order_TEST_{uuid4().hex[:16]}"
    now = int(datetime.now(timezone.utc).timestamp())
    customer_opted_out = random.random() < 0.10
    payment: dict[str, Any] = {
        "id": payment_id,
        "entity": "payment",
        "amount": _amount(min_amount, max_amount),
        "currency": "INR",
        "status": "failed",
        "order_id": order_id,
        "invoice_id": None,
        "international": False,
        "method": method,
        "amount_refunded": 0,
        "refund_status": None,
        "captured": False,
        "description": None,
        "card_id": None,
        "card": None,
        "bank": "TEST_BANK" if method == "netbanking" else None,
        "wallet": "test_wallet" if method == "wallet" else None,
        "vpa": "test@upi" if method == "upi" else None,
        "email": "test-user@example.com",
        "contact": "+919999999999",
        "notes": {"source": "simulator", "product": selected_product, "customer_opted_out": customer_opted_out},
        "fee": None,
        "tax": None,
        "error_code": scenario.get("error_code", "BAD_REQUEST_ERROR"),
        "error_description": scenario.get("error_description", scenario.get("description", "Simulated payment failure.")),
        "error_source": scenario.get("error_source", "simulator"),
        "error_step": scenario.get("error_step", "payment_authorization"),
        "error_reason": scenario.get("error_reason", scenario.get("failure_category", "payment_failed")),
        "acquirer_data": {},
        "created_at": now,
    }
    if method == "card":
        payment["card_id"] = f"card_TEST_{uuid4().hex[:16]}"
        payment["card"] = {"id": payment["card_id"], "entity": "card", "name": "TEST USER", "last4": "0005", "network": "MasterCard", "type": "credit", "issuer": "TEST", "international": False, "emi": False, "sub_type": "consumer"}
    event = {"id": f"evt_TEST_{uuid4().hex[:16]}", "entity": "event", "account_id": "acc_TEST_REVIVE", "event": "payment.failed", "contains": ["payment"], "payload": {"payment": {"entity": payment}}, "created_at": now}
    metadata = {"source": "simulator", "environment": "test", "is_simulated": True, "scenario_id": scenario["id"], "payment_method": method, "product": selected_product, "customer_opted_out": customer_opted_out}
    return event, metadata
