import json
import random

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal, create_tables, get_db
from app.models import Action, AuditLog, Case, HumanReviewQueue, WebhookEvent
from app.recovery_engine import RecoveryEngine
from app.recovery_scheduler import RecoveryScheduler
from app.schemas import ActionResponse, AuditLogResponse, BatchSimulationRequest, BatchSimulationResponse, CaseResponse, HealthResponse, RecoveryResult, SimulationRequest, SimulationResponse, WebhookPayload
from app.security import verify_razorpay_signature
from app.service import process_payment_captured, process_payment_failed, process_payment_link_event
from app.test_event_generator import SimulationError, generate_event, get_scenario, scenarios

app = FastAPI(title="Revive Revenue Recovery API", version="0.1.0")
scheduler: RecoveryScheduler | None = None
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup() -> None:
    create_tables()
    global scheduler
    settings = get_settings()
    if settings.recovery_engine_enabled and settings.environment.lower() in {"development", "dev", "test", "testing"}:
        scheduler = RecoveryScheduler(SessionLocal, RecoveryEngine().run_cycle, settings.recovery_engine_interval_seconds)
        scheduler.start()


@app.on_event("shutdown")
def shutdown() -> None:
    if scheduler:
        scheduler.stop()


@app.get("/")
def root() -> dict[str, str]:
    return {"message": "Revive API is running", "health": "/health", "docs": "/docs"}


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@app.get("/cases", response_model=list[CaseResponse])
def list_cases(db: Session = Depends(get_db)) -> list[Case]:
    return list(db.scalars(select(Case).order_by(Case.created_at.desc())))


@app.get("/cases/{case_id}", response_model=CaseResponse)
def get_case(case_id: str, db: Session = Depends(get_db)) -> Case:
    case = db.get(Case, case_id)
    if not case:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="case not found")
    return case


@app.get("/cases/{case_id}/actions", response_model=list[ActionResponse])
def list_case_actions(case_id: str, db: Session = Depends(get_db)) -> list[Action]:
    if not db.get(Case, case_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="case not found")
    return list(db.scalars(select(Action).where(Action.case_id == case_id).order_by(Action.created_at.desc())))


@app.get("/cases/{case_id}/audit", response_model=list[AuditLogResponse])
def list_case_audit(case_id: str, db: Session = Depends(get_db)) -> list[AuditLog]:
    if not db.get(Case, case_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="case not found")
    return list(db.scalars(select(AuditLog).where(AuditLog.case_id == case_id).order_by(AuditLog.created_at.desc())))


@app.get("/audit", response_model=list[AuditLogResponse])
def list_audit(db: Session = Depends(get_db)) -> list[AuditLog]:
    return list(db.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(100)))


@app.get("/actions", response_model=list[ActionResponse])
def list_actions(db: Session = Depends(get_db)) -> list[Action]:
    return list(db.scalars(select(Action).order_by(Action.created_at.desc()).limit(100)))


@app.get("/human-review")
def list_human_review(db: Session = Depends(get_db)) -> list[dict[str, object]]:
    from app.models import HumanReviewQueue

    entries = db.scalars(select(HumanReviewQueue).where(HumanReviewQueue.reviewed.is_(False)).order_by(HumanReviewQueue.escalated_at.desc())).all()
    return [{"id": entry.id, "case_id": entry.case_id, "reason": entry.reason, "reviewed": entry.reviewed, "escalated_at": entry.escalated_at} for entry in entries]


@app.get("/metrics")
def metrics(db: Session = Depends(get_db)) -> dict[str, object]:
    cases = list(db.scalars(select(Case)).all())
    recovered = [case for case in cases if case.status == "recovered"]
    open_cases = [case for case in cases if case.status in {"open", "retry_1", "retry_2", "retry_3"}]
    total_amount = sum((case.amount for case in cases), 0)
    recovered_amount = sum((case.amount for case in recovered), 0)
    return {
        "total_cases": len(cases),
        "open_cases": len(open_cases),
        "recovered_cases": len(recovered),
        "recovered_amount": str(recovered_amount),
        "at_risk_amount": str(total_amount - recovered_amount),
        "recovery_rate": round((len(recovered) / len(cases)) * 100, 2) if cases else 0,
    }


@app.get("/settings")
def settings_status() -> dict[str, object]:
    settings = get_settings()
    return {
        "environment": settings.environment,
        "recovery_engine_enabled": settings.recovery_engine_enabled,
        "recovery_engine_interval_seconds": settings.recovery_engine_interval_seconds,
        "database": "postgresql",
        "webhook_verification": "enabled",
    }


@app.post("/human-review/{review_id}/resolve")
def resolve_human_review(review_id: int, db: Session = Depends(get_db)) -> dict[str, object]:
    review = db.get(HumanReviewQueue, review_id)
    if not review or review.reviewed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="pending review not found")
    review.reviewed = True
    case = db.get(Case, review.case_id)
    if case:
        db.add(AuditLog(case_id=case.id, event=f"human_review_resolved; review_id={review.id}; new_state=reviewed; actor=frontend"))
    db.commit()
    return {"id": review.id, "case_id": review.case_id, "reviewed": review.reviewed}


def _process_webhook_payload(db: Session, payload: dict, metadata: dict[str, object] | None = None) -> RecoveryResult:
    metadata = metadata or {"source": "razorpay", "is_simulated": False}
    event = payload["event"]
    event_id = str(payload.get("id", ""))
    webhook_event = None
    if event_id:
        webhook_event = db.scalar(select(WebhookEvent).where(WebhookEvent.razorpay_event_id == event_id))
        if not webhook_event:
            webhook_event = WebhookEvent(razorpay_event_id=event_id, event=event, payload=json.dumps(payload, separators=(",", ":")), source=str(metadata.get("source", "razorpay")), is_simulated=bool(metadata.get("is_simulated", False)), scenario_id=metadata.get("scenario_id"), payment_method=metadata.get("payment_method"), product=metadata.get("product"))
            db.add(webhook_event)
            db.commit()
    if event == "payment.captured":
        result = process_payment_captured(db, payload)
    elif event in {"payment_link.paid", "payment_link.partially_paid", "payment_link.expired", "payment_link.cancelled"}:
        result = process_payment_link_event(db, payload)
    elif event == "payment.failed":
        result = process_payment_failed(db, payload)
    else:
        raise HTTPException(status_code=status.HTTP_202_ACCEPTED, detail="event ignored")
    if result is None:
        raise HTTPException(status_code=status.HTTP_202_ACCEPTED, detail="event ignored")
    if webhook_event:
        webhook_event.case_id = result.risk_id
        db.commit()
    return result


@app.post("/webhooks/razorpay", response_model=RecoveryResult)
async def razorpay_webhook(request: Request, x_razorpay_signature: str | None = Header(default=None), db: Session = Depends(get_db)) -> RecoveryResult:
    raw_body = await request.body()
    settings = get_settings()
    if not x_razorpay_signature or not verify_razorpay_signature(raw_body, x_razorpay_signature, settings.razorpay_webhook_secret):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid webhook signature")
    try:
        payload = await request.json()
        WebhookPayload.model_validate(payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid webhook payload") from exc
    return _process_webhook_payload(db, payload)


def _ensure_dev_environment() -> None:
    if get_settings().environment.lower() not in {"development", "dev", "test", "testing"}:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")


def _simulation_response(payload: dict, metadata: dict[str, object], result: RecoveryResult) -> SimulationResponse:
    payment = payload["payload"]["payment"]["entity"]
    return SimulationResponse(scenario_id=str(metadata["scenario_id"]), payment_id=str(payment["id"]), payment_method=str(metadata["payment_method"]), product=str(metadata["product"]), error_code=str(payment["error_code"]), error_reason=str(payment["error_reason"]), case_id=result.risk_id, processing_status="processed")


@app.get("/dev/simulate/scenarios")
def list_simulation_scenarios() -> list[dict]:
    _ensure_dev_environment()
    return scenarios()


@app.get("/dev/simulate/scenarios/{scenario_id}")
def simulation_scenario(scenario_id: str) -> dict:
    _ensure_dev_environment()
    try:
        return get_scenario(scenario_id)
    except SimulationError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


def _simulate(request: SimulationRequest, db: Session) -> SimulationResponse:
    try:
        scenario = get_scenario(request.scenario_id) if request.scenario_id else None
        if scenario is None:
            matching = [item for item in scenarios() if request.product.lower() == "any" or request.product.lower() in item.get("product", [])]
            if request.payment_method.lower() != "any":
                matching = [item for item in matching if item.get("method") in {"any", request.payment_method.lower()}]
            if not matching:
                raise SimulationError("no catalogue scenario matches the requested filters")
            scenario = random.choice(matching)
        payload, metadata = generate_event(scenario, request.payment_method, request.product, request.min_amount, request.max_amount)
        result = _process_webhook_payload(db, payload, metadata)
        print(f"SIMULATED PAYMENT FAILURE scenario_id={metadata['scenario_id']} payment_id={payload['payload']['payment']['entity']['id']} method={metadata['payment_method']} product={metadata['product']} error_code={payload['payload']['payment']['entity']['error_code']} error_reason={payload['payload']['payment']['entity']['error_reason']} source={payload['payload']['payment']['entity']['error_source']} step={payload['payload']['payment']['entity']['error_step']}")
        return _simulation_response(payload, metadata, result)
    except SimulationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc


@app.post("/dev/simulate/failure", response_model=SimulationResponse)
def simulate_failure(request: SimulationRequest, db: Session = Depends(get_db)) -> SimulationResponse:
    _ensure_dev_environment()
    if not request.scenario_id:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="scenario_id is required")
    return _simulate(request, db)


@app.post("/dev/simulate/failure/random", response_model=SimulationResponse)
def simulate_random_failure(request: SimulationRequest, db: Session = Depends(get_db)) -> SimulationResponse:
    _ensure_dev_environment()
    return _simulate(request, db)


@app.post("/dev/simulate/batch", response_model=BatchSimulationResponse)
def simulate_batch(request: BatchSimulationRequest, db: Session = Depends(get_db)) -> BatchSimulationResponse:
    _ensure_dev_environment()
    if not 1 <= request.count <= 100:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="count must be between 1 and 100")
    events = [_simulate(request.model_copy(update={"scenario_id": request.scenario_id}), db) for _ in range(request.count)]
    return BatchSimulationResponse(generated=len(events), successful=len(events), failed=0, events=events)


@app.post("/dev/recovery/run")
def run_recovery_engine(db: Session = Depends(get_db)) -> dict[str, int | str]:
    _ensure_dev_environment()
    processed = RecoveryEngine().run_cycle(db)
    return {"status": "completed", "processed": processed}
