from collections.abc import Generator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_database_url


class Base(DeclarativeBase):
    pass


_engine = create_engine(get_database_url(), pool_pre_ping=True)
SessionLocal = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_tables() -> None:
    from app import models

    Base.metadata.create_all(_engine)
    columns = {column["name"] for column in inspect(_engine).get_columns("cases")}
    new_columns = {
        "payment_method": "VARCHAR(32)",
        "order_id": "VARCHAR(120)",
        "error_description": "TEXT",
        "error_source": "VARCHAR(32)",
        "error_step": "VARCHAR(64)",
        "retry_count": "INTEGER NOT NULL DEFAULT 0",
        "last_retry_at": "TIMESTAMP WITH TIME ZONE",
        "next_retry_at": "TIMESTAMP WITH TIME ZONE",
        "next_action_at": "TIMESTAMP WITH TIME ZONE",
        "product": "VARCHAR(32)",
        "customer_opted_out": "BOOLEAN NOT NULL DEFAULT FALSE",
    }
    webhook_columns = {
        "source": "VARCHAR(32) NOT NULL DEFAULT 'razorpay'",
        "is_simulated": "BOOLEAN NOT NULL DEFAULT FALSE",
        "scenario_id": "VARCHAR(32)",
        "payment_method": "VARCHAR(32)",
        "product": "VARCHAR(32)",
    }
    with _engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            connection.execute(text("ALTER TABLE cases DROP CONSTRAINT IF EXISTS ck_cases_status"))
            connection.execute(text("ALTER TABLE cases ADD CONSTRAINT ck_cases_status CHECK (status IN ('open', 'retry_1', 'retry_2', 'retry_3', 'recovered', 'escalated', 'closed_lost'))"))
        for column_name, column_type in new_columns.items():
            if column_name not in columns:
                connection.execute(text(f"ALTER TABLE cases ADD COLUMN {column_name} {column_type}"))
        webhook_columns_existing = {column["name"] for column in inspect(_engine).get_columns("webhook_events")}
        for column_name, column_type in webhook_columns.items():
            if column_name not in webhook_columns_existing:
                connection.execute(text(f"ALTER TABLE webhook_events ADD COLUMN {column_name} {column_type}"))
        if "order_id" not in columns:
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_cases_order_id ON cases (order_id)"))
