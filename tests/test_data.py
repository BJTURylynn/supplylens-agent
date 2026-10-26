import importlib.util
import json
from copy import deepcopy
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import DateTime, create_mock_engine, func, select
from sqlalchemy.dialects import sqlite
from sqlalchemy.orm import Session

from supplylens.db import (
    Base,
    ETAHistory,
    LogisticsEvent,
    PurchaseOrder,
    Receipt,
    Run,
    Shipment,
    Supplier,
    import_seed,
    init_database,
    load_run,
    make_engine,
    save_run,
    validate_seed,
)
from supplylens.repository import OrderNotFound, Repository

ROOT = Path(__file__).resolve().parents[1]
SEED = json.loads((ROOT / "data" / "seed.json").read_text(encoding="utf-8"))


@pytest.fixture
def engine():
    result = make_engine("sqlite:///:memory:")
    init_database(result)
    yield result
    result.dispose()


def test_seed_shape_and_reproducible_generator():
    validate_seed(SEED)
    assert SEED["synthetic"] is True
    assert len(SEED["orders"]) == 100
    assert len(SEED["suppliers"]) == 10
    spec = importlib.util.spec_from_file_location("seed_generator", ROOT / "scripts" / "generate_seed.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.build_seed() == SEED


def test_reimport_is_idempotent_and_preserves_runs(engine):
    payload = {"run_id": "run-test", "status": "completed", "report": {"order_id": "PO-1001"}}
    save_run(engine, payload)
    init_database(engine)
    with Session(engine) as session:
        for model, key in ((Supplier, "suppliers"), (PurchaseOrder, "orders"), (Shipment, "shipments"),
                           (ETAHistory, "eta_history"), (LogisticsEvent, "logistics_events"), (Receipt, "receipts")):
            assert session.scalar(select(func.count()).select_from(model)) == len(SEED[key])
        assert session.scalar(select(func.count()).select_from(Run)) == 1
    assert load_run(engine, "run-test")["report"] == payload["report"]


def test_snapshot_hides_future_eta_events_and_receipts(engine):
    snapshot = Repository(engine, date(2026, 10, 24)).get_order_snapshot("PO-1011")
    assert snapshot["shipments"][0]["eta"] == "2026-10-24"
    assert snapshot["shipments"][0]["latest_event"]["status"] == "in_transit"
    assert snapshot["receipts"] == []
    serialized = json.dumps(snapshot)
    assert "ETA-1011-A-2" not in serialized
    assert "EV-1011-A-2" not in serialized
    assert "RC-1011-A-1" not in serialized
    tomorrow = Repository(engine, date(2026, 10, 25)).get_order_snapshot("PO-1011")
    assert tomorrow["shipments"][0]["eta"] == "2026-10-29"
    assert tomorrow["shipments"][0]["latest_event"]["status"] == "held"
    assert tomorrow["receipts"][0]["qty"] == 100


def test_end_of_day_inclusion_and_next_midnight_exclusion(engine):
    snapshot = Repository(engine, date(2026, 10, 24)).get_order_snapshot("PO-1012")
    assert [row["qty"] for row in snapshot["receipts"]] == [100]
    assert snapshot["shipments"][0]["latest_event"]["event_time"] == "2026-10-24T23:59:59+08:00"


def test_utc_timestamp_is_compared_as_shanghai_time(engine):
    data = deepcopy(SEED)
    row = next(row for row in data["receipts"] if row["id"] == "RC-1012-A-1")
    row["received_at"] = "2026-10-24T15:59:59Z"
    import_seed(engine, data)
    snapshot = Repository(engine, date(2026, 10, 24)).get_order_snapshot("PO-1012")
    assert snapshot["receipts"][0]["received_at"] == "2026-10-24T23:59:59+08:00"


def test_eta_tie_break_and_latest_event_are_deterministic(engine):
    data = deepcopy(SEED)
    data["eta_history"].append({"id": "ETA-1002-A-Z", "shipment_id": "SH-1002-A", "eta": "2026-10-30",
                                "updated_at": "2026-10-20T09:00:00+08:00"})
    data["logistics_events"].append({"id": "EV-1002-A-Z", "shipment_id": "SH-1002-A", "status": "delayed",
                                     "event_time": "2026-10-23T10:00:00+08:00", "note": "模拟同时间事件。"})
    import_seed(engine, data)
    shipment = Repository(engine, date(2026, 10, 24)).get_order_snapshot("PO-1002")["shipments"][0]
    assert shipment["eta"] == "2026-10-30"
    assert shipment["latest_event"]["id"] == "EV-1002-A-Z"


def test_missing_order_and_future_order_are_not_visible(engine):
    repository = Repository(engine, date(2026, 10, 24))
    with pytest.raises(OrderNotFound):
        repository.get_order_snapshot("PO-NOT-FOUND")
    old = Repository(engine, date(2026, 9, 30))
    assert old.list_orders() == []
    with pytest.raises(OrderNotFound):
        old.get_order_snapshot("PO-1001")


@pytest.mark.parametrize("case", ["duplicate", "foreign_key", "zero_qty", "bool_qty", "overship", "overreceipt", "early_receipt", "bad_status", "bad_date"])
def test_bad_seed_rejected_before_import(engine, case):
    data = deepcopy(SEED)
    if case == "duplicate":
        data["orders"].append(deepcopy(data["orders"][0]))
    elif case == "foreign_key":
        data["receipts"][0]["shipment_id"] = "missing"
    elif case == "zero_qty":
        data["orders"][0]["qty"] = 0
    elif case == "bool_qty":
        data["orders"][0]["qty"] = True
    elif case == "overship":
        data["shipments"][0]["qty"] = 1001
    elif case == "overreceipt":
        data["receipts"][0]["qty"] = 601
    elif case == "early_receipt":
        data["receipts"][0]["received_at"] = "2026-09-01T00:00:00+08:00"
    elif case == "bad_status":
        data["logistics_events"][0]["status"] = "unknown"
    elif case == "bad_date":
        data["orders"][0]["promised_date"] = "not-a-date"
    with pytest.raises(ValueError):
        import_seed(engine, data)
    assert Repository(engine, date(2026, 10, 24)).get_order_snapshot("PO-1001")["ordered_qty"] == 1000


def test_run_roundtrip_and_update(engine):
    assert load_run(engine, "missing") is None
    save_run(engine, {"run_id": "run-1", "status": "partial", "steps": [], "created_at": "2026-10-24T12:00:00+08:00"})
    save_run(engine, {"run_id": "run-1", "status": "completed", "steps": [{"result": "含中文"}], "created_at": "2026-10-24T12:00:00+08:00"})
    result = load_run(engine, "run-1")
    assert result["status"] == "completed"
    assert result["steps"][0]["result"] == "含中文"
    assert result["created_at"] == "2026-10-24T12:00:00+08:00"


def test_mysql_schema_compiles_without_sqlite_only_types():
    statements = []
    engine = create_mock_engine("mysql+pymysql://", lambda sql, *args, **kwargs: statements.append(str(sql.compile(dialect=engine.dialect))))
    Base.metadata.create_all(engine)
    assert any("CREATE TABLE purchase_orders" in statement for statement in statements)
    assert any("CREATE TABLE eta_history" in statement for statement in statements)
    assert any("JSON" in statement for statement in statements)
    datetime_columns = [column for table in Base.metadata.tables.values() for column in table.columns
                        if isinstance(column.type, DateTime)]
    assert len(datetime_columns) == 7
    sqlite_dialect = sqlite.dialect()
    for column in datetime_columns:
        assert column.type.compile(dialect=engine.dialect) == "DATETIME(6)", column
        assert column.type.compile(dialect=sqlite_dialect) == "DATETIME", column
