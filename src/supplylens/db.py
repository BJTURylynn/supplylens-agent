"""Portable business storage. All naive SQL datetimes represent Asia/Shanghai."""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import JSON, Date, DateTime, ForeignKey, Integer, String, Text, create_engine, event
from sqlalchemy.dialects.mysql import DATETIME as MySQLDateTime
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from sqlalchemy.pool import StaticPool

SHANGHAI = timezone(timedelta(hours=8))
PRECISE_DATETIME = DateTime().with_variant(MySQLDateTime(fsp=6), "mysql")
EVENT_STATUSES = {"departed", "in_transit", "delayed", "held", "customs_hold", "exception", "delivered", "arrived"}


class Base(DeclarativeBase):
    pass


class Supplier(Base):
    __tablename__ = "suppliers"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    supplier_id: Mapped[str] = mapped_column(ForeignKey("suppliers.id"), index=True)
    material_code: Mapped[str] = mapped_column(String(64))
    qty: Mapped[int] = mapped_column(Integer)
    promised_date: Mapped[date] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME)


class Shipment(Base):
    __tablename__ = "shipments"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("purchase_orders.id"), index=True)
    qty: Mapped[int] = mapped_column(Integer)
    shipped_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME)


class ETAHistory(Base):
    __tablename__ = "eta_history"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    shipment_id: Mapped[str] = mapped_column(ForeignKey("shipments.id"), index=True)
    eta: Mapped[date] = mapped_column(Date)
    updated_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME)


class LogisticsEvent(Base):
    __tablename__ = "logistics_events"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    shipment_id: Mapped[str] = mapped_column(ForeignKey("shipments.id"), index=True)
    status: Mapped[str] = mapped_column(String(32))
    event_time: Mapped[datetime] = mapped_column(PRECISE_DATETIME)
    note: Mapped[str] = mapped_column(Text)


class Receipt(Base):
    __tablename__ = "receipts"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    shipment_id: Mapped[str] = mapped_column(ForeignKey("shipments.id"), index=True)
    qty: Mapped[int] = mapped_column(Integer)
    received_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME)


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME)
    payload: Mapped[dict] = mapped_column(JSON)


class OrderOrigin(Base):
    """User-import provenance and explicitly provided source-table coverage."""

    __tablename__ = "order_origins"
    order_id: Mapped[str] = mapped_column(ForeignKey("purchase_orders.id"), primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(64))
    source_name: Mapped[str] = mapped_column(Text)
    imported_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME)
    as_of: Mapped[date] = mapped_column(Date)
    coverage: Mapped[dict] = mapped_column(JSON)


def local_datetime(value: str | datetime) -> datetime:
    """ISO input, with optional offset; database stores Shanghai wall-clock time."""
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if result.tzinfo is not None:
        result = result.astimezone(SHANGHAI).replace(tzinfo=None)
    return result


def iso_datetime(value: datetime) -> str:
    return value.replace(tzinfo=SHANGHAI).isoformat()


def cutoff_after(as_of: date) -> datetime:
    """Exclusive next midnight includes every microsecond of the selected day."""
    return datetime.combine(as_of + timedelta(days=1), time.min)


def make_engine(database_url: str) -> Engine:
    kwargs: dict[str, Any] = {"pool_pre_ping": True}
    if database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if database_url.endswith(":memory:") or database_url in {"sqlite://", "sqlite+pysqlite://"}:
            kwargs["poolclass"] = StaticPool
    engine = create_engine(database_url, **kwargs)
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def sqlite_foreign_keys(connection: Any, _record: Any) -> None:
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    return engine


def _positive_int(value: Any, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")


def _text(value: Any, field: str, maximum: int = 64) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{field} must be nonempty text of at most {maximum} characters")


def validate_seed(data: dict) -> None:
    """Validate a complete synthetic seed BEFORE any database writes.

    Raises ValueError for invalid IDs, relations, quantities, timestamps or statuses.
    Cancellation, returns, quantity amendments and over-receipt are outside this MVP.
    """
    tables = ("suppliers", "orders", "shipments", "eta_history", "logistics_events", "receipts")
    indexed: dict[str, dict[str, dict]] = {}
    try:
        for table in tables:
            rows = data[table]
            if not isinstance(rows, list):
                raise TypeError(f"{table} must be a list")
            records = {}
            for row in rows:
                _text(row["id"], f"{table}.id")
                if row["id"] in records:
                    raise ValueError(f"duplicate {table} ID: {row['id']}")
                records[row["id"]] = row
            indexed[table] = records
        for row in data["suppliers"]:
            _text(row["name"], "supplier.name", 200)
        for row in data["orders"]:
            if row["supplier_id"] not in indexed["suppliers"]:
                raise ValueError(f"unknown supplier: {row['supplier_id']}")
            _text(row["material_code"], "order.material_code")
            _positive_int(row["qty"], "order.qty")
            promised = date.fromisoformat(row["promised_date"])
            created = local_datetime(row["created_at"])
            if promised < created.date():
                raise ValueError(f"promise precedes creation: {row['id']}")
        allocated: dict[str, int] = {}
        for row in data["shipments"]:
            order = indexed["orders"].get(row["order_id"])
            if order is None:
                raise ValueError(f"unknown shipment order: {row['order_id']}")
            _positive_int(row["qty"], "shipment.qty")
            if local_datetime(row["shipped_at"]) < local_datetime(order["created_at"]):
                raise ValueError(f"shipment precedes order creation: {row['id']}")
            allocated[row["order_id"]] = allocated.get(row["order_id"], 0) + row["qty"]
        for order_id, quantity in allocated.items():
            if quantity > indexed["orders"][order_id]["qty"]:
                raise ValueError(f"shipments exceed ordered quantity: {order_id}")
        received: dict[str, int] = {}
        for table, timestamp in (("eta_history", "updated_at"), ("logistics_events", "event_time"), ("receipts", "received_at")):
            for row in data[table]:
                shipment = indexed["shipments"].get(row["shipment_id"])
                if shipment is None:
                    raise ValueError(f"unknown {table} shipment: {row['shipment_id']}")
                if local_datetime(row[timestamp]) < local_datetime(shipment["shipped_at"]):
                    raise ValueError(f"{table} record precedes shipment: {row['id']}")
                if table == "eta_history":
                    if date.fromisoformat(row["eta"]) < local_datetime(shipment["shipped_at"]).date():
                        raise ValueError(f"ETA precedes shipment: {row['id']}")
                elif table == "logistics_events":
                    if row["status"] not in EVENT_STATUSES:
                        raise ValueError(f"unsupported logistics status: {row['status']}")
                    if not isinstance(row.get("note"), str):
                        raise ValueError("event.note must be a string")
                else:
                    _positive_int(row["qty"], "receipt.qty")
                    received[row["shipment_id"]] = received.get(row["shipment_id"], 0) + row["qty"]
        for shipment_id, quantity in received.items():
            if quantity > indexed["shipments"][shipment_id]["qty"]:
                raise ValueError(f"receipts exceed shipment quantity: {shipment_id}")
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"malformed seed data: {exc}") from exc


def import_seed(engine: Engine, data: dict) -> None:
    """Idempotent upsert by stable IDs, preserving unrelated runs and records."""
    validate_seed(data)
    models = (("suppliers", Supplier), ("orders", PurchaseOrder), ("shipments", Shipment),
              ("eta_history", ETAHistory), ("logistics_events", LogisticsEvent), ("receipts", Receipt))
    date_fields = {"promised_date", "eta"}
    datetime_fields = {"created_at", "shipped_at", "updated_at", "event_time", "received_at"}
    with Session(engine) as session, session.begin():
        for table, model in models:
            for row in data[table]:
                values = {key: date.fromisoformat(value) if key in date_fields else
                          local_datetime(value) if key in datetime_fields else value
                          for key, value in row.items()}
                session.merge(model(**values))
            session.flush()


def init_database(engine: Engine, seed: bool = True) -> None:
    Base.metadata.create_all(engine)
    if seed:
        # Re-starts must never reapply demo rows over imported/current business data.
        with Session(engine) as session:
            populated = session.query(PurchaseOrder.id).first() is not None
        if not populated:
            seed_path = Path(__file__).resolve().parents[2] / "data" / "seed.json"
            import_seed(engine, json.loads(seed_path.read_text(encoding="utf-8")))


def save_run(engine: Engine, payload: dict) -> None:
    """Persist JSON-safe report and trace; repeat saves replace the same run ID."""
    run_id = payload.get("run_id")
    _text(run_id, "run_id")
    saved = json.loads(json.dumps(payload, ensure_ascii=False))
    created = local_datetime(saved.get("created_at") or datetime.now(SHANGHAI))
    saved.setdefault("created_at", iso_datetime(created))
    with Session(engine) as session, session.begin():
        session.merge(Run(id=run_id, created_at=created, payload=saved))


def load_run(engine: Engine, run_id: str) -> dict | None:
    with Session(engine) as session:
        run = session.get(Run, run_id)
        return json.loads(json.dumps(run.payload, ensure_ascii=False)) if run else None
