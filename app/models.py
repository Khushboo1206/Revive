from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class CaseStatus(StrEnum):
    OPEN = "open"
    RETRY_1 = "retry_1"
    RETRY_2 = "retry_2"
    RETRY_3 = "retry_3"
    RECOVERED = "recovered"
    ESCALATED = "escalated"
    CLOSED_LOST = "closed_lost"


class ActionType(StrEnum):
    REGENERATE_LINK = "regenerate_link"
    SEND_REMINDER = "send_reminder"
    ESCALATE_TO_HUMAN = "escalate_to_human"
    NO_ACTION = "no_action"


class PromiseStatus(StrEnum):
    PENDING = "pending"
    KEPT = "kept"
    BROKEN = "broken"


class ReviewReason(StrEnum):
    CAPS_EXCEEDED = "caps_exceeded"
    OPT_OUT = "opt_out"
    BROKEN_PROMISE = "broken_promise"


class Case(Base):
    __tablename__ = "cases"
    __table_args__ = (
        CheckConstraint("source_type IN ('checkout_dropoff', 'subscription_failed', 'invoice_overdue')", name="ck_cases_source_type"),
        CheckConstraint("status IN ('open', 'retry_1', 'retry_2', 'retry_3', 'recovered', 'escalated', 'closed_lost')", name="ck_cases_status"),
    )

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    customer_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    raw_error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    root_cause: Mapped[str | None] = mapped_column(String(120), nullable=True)
    payment_method: Mapped[str | None] = mapped_column(String(32), nullable=True)
    order_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    error_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error_step: Mapped[str | None] = mapped_column(String(64), nullable=True)
    product: Mapped[str | None] = mapped_column(String(32), nullable=True)
    customer_opted_out: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_action_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[CaseStatus] = mapped_column(String(20), default=CaseStatus.OPEN, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Action(Base):
    __tablename__ = "actions"
    __table_args__ = (
        CheckConstraint("action_type IN ('regenerate_link', 'send_reminder', 'escalate_to_human', 'no_action')", name="ck_actions_type"),
        CheckConstraint("channel IS NULL OR channel IN ('email', 'sms', 'whatsapp')", name="ck_actions_channel"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    action_type: Mapped[ActionType] = mapped_column(String(32), nullable=False)
    channel: Mapped[str | None] = mapped_column(String(20), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Promise(Base):
    __tablename__ = "promises"
    __table_args__ = (CheckConstraint("status IN ('pending', 'kept', 'broken')", name="ck_promises_status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    promised_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[PromiseStatus] = mapped_column(String(20), default=PromiseStatus.PENDING, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    event: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class HumanReviewQueue(Base):
    __tablename__ = "human_review_queue"
    __table_args__ = (CheckConstraint("reason IN ('caps_exceeded', 'opt_out', 'broken_promise')", name="ck_review_reason"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    reason: Mapped[ReviewReason] = mapped_column(String(32), nullable=False)
    reviewed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    escalated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WebhookEvent(Base):
    __tablename__ = "webhook_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    razorpay_event_id: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    event: Mapped[str] = mapped_column(String(120), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(32), default="razorpay", nullable=False)
    is_simulated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    scenario_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    payment_method: Mapped[str | None] = mapped_column(String(32), nullable=True)
    product: Mapped[str | None] = mapped_column(String(32), nullable=True)
    case_id: Mapped[str | None] = mapped_column(ForeignKey("cases.id", ondelete="SET NULL"), nullable=True, index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
