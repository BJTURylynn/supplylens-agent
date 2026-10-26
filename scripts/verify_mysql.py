"""Verify SupplyLens against a new, isolated MySQL schema; never drop a database.

Set MYSQL_VERIFICATION_URL to a mysql+pymysql server URL with CREATE DATABASE
permission. Existing URL database components are never used. No model is called.
Run: python scripts/verify_mysql.py --output evaluation/results/mysql.json
Credentials are read from the environment, never printed or put in the report.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import os
import re
import tempfile
import time
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from supplylens.agent import AgentService
from supplylens.config import ROOT, Settings
from supplylens.db import (
    SHANGHAI,
    Base,
    PurchaseOrder,
    Receipt,
    Run,
    Shipment,
    Supplier,
    init_database,
    load_run,
    make_engine,
    save_run,
)
from supplylens.imports import TABLE_FIELDS, ImportService, parse_uploads
from supplylens.repository import Repository
from supplylens.rules import analyze_arrival

AS_OF = date(2026, 10, 24)
SCHEMA_PATTERN = re.compile(r"supplylens_verification_[0-9]{8}_[a-f0-9]{12}\Z")


class VerificationFailure(RuntimeError):
    """A fixed, credential-free verification failure."""


def _require(condition: bool) -> None:
    if not condition:
        raise VerificationFailure("Verification invariant failed")


def _count(engine: Engine, model) -> int:
    with Session(engine) as session:
        return session.scalar(select(func.count()).select_from(model))


def _csv_files(data: dict) -> list[tuple[str, bytes]]:
    result = []
    for table, columns in TABLE_FIELDS.items():
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=columns)
        writer.writeheader()
        writer.writerows(data[table])
        result.append((table + ".csv", output.getvalue().encode("utf-8-sig")))
    return result


def verify_application(engine: Engine, checks: list[dict]) -> None:
    """Exercise actual storage/rules/import/agent code on the supplied empty DB.

    SQLite is allowed here only for offline tests; the CLI requires MySQL.
    Each successful check is recorded only after all its invariants pass.
    """
    def stage(name, operation):
        started = time.monotonic()
        try:
            detail = operation()
        except Exception as exc:
            checks.append({"name": name, "status": "failed", "error_type": type(exc).__name__,
                           "elapsed_ms": round((time.monotonic() - started) * 1000, 2)})
            raise
        checks.append({"name": name, "status": "passed", "detail": detail,
                       "elapsed_ms": round((time.monotonic() - started) * 1000, 2)})

    def tables():
        init_database(engine, seed=False)
        actual = sorted(inspect(engine).get_table_names())
        _require(set(actual) == set(Base.metadata.tables))
        return {"tables": actual, "table_count": len(actual)}

    stage("create_tables", tables)

    def seed():
        init_database(engine, seed=True)
        counts = {"purchase_orders": _count(engine, PurchaseOrder), "suppliers": _count(engine, Supplier)}
        _require(counts == {"purchase_orders": 100, "suppliers": 10})
        return counts

    stage("import_synthetic_seed", seed)
    stage("idempotent_seed_restart", seed)

    def standard_order():
        analysis = analyze_arrival(Repository(engine, AS_OF).get_order_snapshot("PO-1001"))
        expected = {"ordered_qty": 1000, "received_qty": 600, "outstanding_qty": 400, "overdue_days": 2}
        facts = {key: analysis["facts"][key] for key in expected}
        _require(facts == expected)
        codes = sorted(row["code"] for row in analysis["anomalies"])
        _require(codes == ["LOGISTICS_EXCEPTION", "OVERDUE_UNRECEIVED"])
        return {"order_id": "PO-1001", "facts": facts, "anomaly_codes": codes}

    stage("query_and_analyze_standard_order", standard_order)
    template = ROOT / "data/templates/supplylens-import-template.xlsx"

    def excel_import():
        service = ImportService(engine)
        preview = service.preview([(template.name, template.read_bytes())], AS_OF)
        _require(preview["new_orders"] == 1 and _count(engine, PurchaseOrder) == 100)
        service.commit(preview["preview_id"])
        analysis = analyze_arrival(Repository(engine, AS_OF).get_order_snapshot("PO-9001"))
        _require(analysis["facts"]["outstanding_qty"] == 400)
        _require(analysis["data_source"]["kind"] == "imported")
        _require(_count(engine, PurchaseOrder) == 101 and _count(engine, Supplier) == 11)
        return {"order_id": "PO-9001", "preview_was_read_only": True,
                "outstanding_qty": 400, "source": "imported"}

    stage("xlsx_preview_and_import", excel_import)

    def csv_update():
        data, _, _ = parse_uploads([(template.name, template.read_bytes())])
        data["receipts"][0]["qty"] = 650
        service = ImportService(engine)
        preview = service.preview(_csv_files(data), AS_OF)
        _require(preview["replace_orders"] == 1 and preview["new_orders"] == 0)
        service.commit(preview["preview_id"])
        analysis = analyze_arrival(Repository(engine, AS_OF).get_order_snapshot("PO-9001"))
        _require(analysis["facts"]["received_qty"] == 650 and analysis["facts"]["outstanding_qty"] == 350)
        with Session(engine) as session:
            receipt_count = session.scalar(select(func.count()).select_from(Receipt).where(Receipt.shipment_id == "USER-SHIP-01"))
        _require(receipt_count == 1 and _count(engine, PurchaseOrder) == 101)
        init_database(engine, seed=True)
        _require(analyze_arrival(Repository(engine, AS_OF).get_order_snapshot("PO-9001"))["facts"]["received_qty"] == 650)
        return {"order_id": "PO-9001", "received_qty": 650, "outstanding_qty": 350,
                "replacement_not_duplicate": True, "restart_preserves_import": True}

    stage("csv_snapshot_update_and_restart", csv_update)

    def rollback():
        rejected = False
        try:
            with Session(engine) as session, session.begin():
                session.add(Supplier(id="VERIFY-ROLLBACK-SUP", name="模拟事务回滚供应商"))
                session.flush()
                session.add(Shipment(id="VERIFY-ORPHAN-SHIP", order_id="VERIFY-MISSING-ORDER", qty=1,
                                     shipped_at=datetime(2026, 10, 20, 9)))
                session.flush()
        except IntegrityError:
            rejected = True
        with Session(engine) as session:
            _require(rejected and session.get(Supplier, "VERIFY-ROLLBACK-SUP") is None)
            _require(session.get(Shipment, "VERIFY-ORPHAN-SHIP") is None)
        return {"orphan_shipment_rejected": True, "earlier_supplier_insert_rolled_back": True}

    stage("database_foreign_key_and_transaction_rollback", rollback)

    def agent_roundtrip():
        with tempfile.TemporaryDirectory(prefix="supplylens-mysql-sop-") as temporary:
            config = Settings(_env_file=None, agent_mode="demo", embedding_mode="local", model_api_key="",
                              as_of_date=AS_OF, index_dir=Path(temporary))
            result = asyncio.run(AgentService(engine, config).run("分析 PO-1001 的到货情况并给出处理建议"))
        _require(result["status"] == "completed" and result["mode"] == "demo")
        _require(result["report"]["facts"]["outstanding_qty"] == 400)
        _require(result["metrics"]["model_rounds"] == 0)
        save_run(engine, result)
        loaded = load_run(engine, result["run_id"])
        _require(loaded == result and _count(engine, Run) == 1)
        _require(loaded["report"]["facts"]["supplier_name"] == "模拟供应商01")
        return {"status": result["status"], "mode": "demo", "tool_calls": result["metrics"]["tool_calls"],
                "model_calls": 0, "run_json_roundtrip": True, "idempotent_save": True,
                "chinese_text_roundtrip": True}

    stage("agent_demo_and_run_persistence", agent_roundtrip)


def run_verification(server_url: str) -> dict:
    started = time.monotonic()
    report = {"format_version": 1, "checked_at": datetime.now(SHANGHAI).isoformat(),
              "as_of": AS_OF.isoformat(), "status": "failed", "driver": "mysql+pymysql",
              "data_kind": "synthetic", "live_model_calls": 0, "checks": []}
    server = engine = None
    try:
        url = make_url(server_url)
        if url.drivername != "mysql+pymysql" or not url.host:
            raise VerificationFailure("Use a mysql+pymysql server URL")
        schema = "supplylens_verification_" + datetime.now(SHANGHAI).strftime("%Y%m%d") + "_" + uuid4().hex[:12]
        _require(bool(SCHEMA_PATTERN.fullmatch(schema)))
        # Never use the supplied URL's database component or IF NOT EXISTS: this
        # invocation must create its own fresh schema, with no existing contents.
        server = create_engine(url.set(database=""), pool_pre_ping=True,
                               connect_args={"connect_timeout": 10})
        with server.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            version = str(connection.execute(text("SELECT VERSION()")).scalar_one())
            report["server_version"] = version
            if "mariadb" in version.lower():
                raise VerificationFailure("This verification requires MySQL rather than MariaDB")
            connection.execute(text(f"CREATE DATABASE `{schema}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"))
        report.update(schema=schema, schema_retained_for_inspection=True)
        schema_url = url.set(database=schema).update_query_dict({"charset": "utf8mb4"})
        engine = make_engine(schema_url.render_as_string(hide_password=False))
        verify_application(engine, report["checks"])
        report["status"] = "passed"
    except Exception as exc:
        # Never serialize driver exception text, SQL parameters, URLs or paths.
        report["failure"] = {"error_type": type(exc).__name__,
                             "message": "Verification did not complete; inspect local server setup and failed check."}
    finally:
        if engine is not None:
            engine.dispose()
        if server is not None:
            server.dispose()
    report["elapsed_ms"] = round((time.monotonic() - started) * 1000, 2)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("evaluation/results/mysql.json"))
    args = parser.parse_args(argv)
    report = run_verification(os.getenv("MYSQL_VERIFICATION_URL", ""))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
