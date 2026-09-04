from decimal import Decimal
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.models import CaseStatus


class RecoveryResult(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    risk_id: str
    event_id: str
    status: CaseStatus
    approved: bool
    strategy: str
    reason: str
    recovery_probability: Decimal


class HealthResponse(BaseModel):
    status: str


class CaseResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    customer_id: str | None
    amount: Decimal
    source_type: str
    raw_error_code: str | None
    root_cause: str | None
    payment_method: str | None
    order_id: str | None
    error_description: str | None
    error_source: str | None
    error_step: str | None
    product: str | None
    customer_opted_out: bool
    retry_count: int
    last_retry_at: datetime | None
    next_retry_at: datetime | None
    next_action_at: datetime | None
    status: CaseStatus
    created_at: datetime


class ActionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    case_id: str
    action_type: str
    channel: str | None
    message: str | None
    agent_reasoning: str | None
    created_at: datetime


class AuditLogResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    case_id: str
    event: str
    created_at: datetime


class WebhookPayload(BaseModel):
    event: str
    payload: dict[str, Any]


class SimulationRequest(BaseModel):
    scenario_id: str | None = None
    payment_method: str = "any"
    product: str = "any"
    min_amount: int = 100
    max_amount: int = 100000


class BatchSimulationRequest(SimulationRequest):
    count: int = 10


class SimulationResponse(BaseModel):
    success: bool = True
    simulated: bool = True
    scenario_id: str
    payment_id: str
    payment_method: str
    product: str
    error_code: str
    error_reason: str
    case_id: str
    processing_status: str


class BatchSimulationResponse(BaseModel):
    generated: int
    successful: int
    failed: int
    events: list[SimulationResponse]
