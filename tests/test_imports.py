import csv
import io
import time
from copy import deepcopy
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from supplylens.agent import AgentService
from supplylens.api import create_app
from supplylens.config import ROOT, Settings
from supplylens.db import PurchaseOrder, Receipt, init_database, load_run, make_engine
from supplylens.imports import (
    TABLE_FIELDS,
    ImportConflict,
    ImportService,
    ImportValidationError,
    parse_uploads,
)
from supplylens.repository import OrderNotFound, Repository
from supplylens.rules import analyze_arrival

TEMPLATES = ROOT / "data/templates"
AS_OF = date(2026, 10, 24)


def sample_data():
    data, _, _ = parse_uploads([(path.name, path.read_bytes()) for path in sorted(TEMPLATES.glob("*.csv"))])
    return data


def csv_files(data, omitted=(), aliases=None):
    result = []
    for table, fields in TABLE_FIELDS.items():
        if table in omitted:
            continue
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow([(aliases or {}).get((table, field), field) for field in fields])
        for row in data[table]:
            writer.writerow([row.get(field, "") for field in fields])
        result.append((table + ".csv", output.getvalue().encode("utf-8-sig")))
    return result


@pytest.fixture
def engine():
    db = make_engine("sqlite:///:memory:")
    init_database(db)
    yield db
    db.dispose()


def commit_sample(engine, data=None, omitted=()):
    importer = ImportService(engine)
    preview = importer.preview(csv_files(data or sample_data(), omitted), AS_OF)
    return importer.commit(preview["preview_id"])


def test_real_excel_template_matches_csv_values_and_timezone():
    path = TEMPLATES / "supplylens-import-template.xlsx"
    excel, coverage, warnings = parse_uploads([(path.name, path.read_bytes())])
    assert excel == sample_data()
    assert all(coverage.values()) and warnings == []
    assert excel["orders"][0]["created_at"] == "2026-10-01T09:00:00+08:00"
    assert excel["receipts"][0]["received_at"] == "2026-10-22T15:00:00+08:00"


def test_preview_has_no_business_writes_and_commit_is_attributed(engine):
    service = ImportService(engine)
    preview = service.preview(csv_files(sample_data()), AS_OF)
    assert preview["new_orders"] == 1 and preview["replace_orders"] == 0
    assert preview["counts"]["receipts"] == 1
    with pytest.raises(OrderNotFound):
        Repository(engine, AS_OF).get_order_snapshot("PO-9001")
    result = service.commit(preview["preview_id"])
    report = analyze_arrival(Repository(engine, AS_OF).get_order_snapshot("PO-9001"))
    assert report["facts"]["ordered_qty"] == 1000
    assert report["facts"]["received_qty"] == 600
    assert report["facts"]["outstanding_qty"] == 400
    assert report["facts"]["overdue_days"] == 2
    assert {row["code"] for row in report["anomalies"]} == {"OVERDUE_UNRECEIVED", "LOGISTICS_EXCEPTION"}
    assert report["data_source"]["kind"] == "imported"
    assert report["data_source"]["batch_id"] == result["batch_id"]
    assert any(row["source"] == "order_origins" for row in report["evidence"])
    with pytest.raises(ImportConflict, match="失效"):
        service.commit(preview["preview_id"])


@pytest.mark.parametrize("quantity", [-1, 0, 1.5, "NaN", "Infinity", "=1+1", "2147483648"])
def test_bad_quantities_rejected(engine, quantity):
    data = sample_data()
    data["orders"][0]["qty"] = quantity
    with pytest.raises(ImportValidationError):
        ImportService(engine).preview(csv_files(data), AS_OF)
    with Session(engine) as session:
        assert session.get(PurchaseOrder, "PO-9001") is None


@pytest.mark.parametrize("mutation", ["duplicate", "foreign_key", "excess_receipt", "bad_status", "bad_time"])
def test_business_validation_rejects_bad_snapshot(engine, mutation):
    data = sample_data()
    if mutation == "duplicate":
        data["orders"].append(deepcopy(data["orders"][0]))
    elif mutation == "foreign_key":
        data["receipts"][0]["shipment_id"] = "MISSING"
    elif mutation == "excess_receipt":
        data["receipts"][0]["qty"] = 1001
    elif mutation == "bad_status":
        data["logistics_events"][0]["status"] = "guessing"
    else:
        data["receipts"][0]["received_at"] = "2026-09-01T00:00:00+08:00"
    with pytest.raises(ImportValidationError):
        ImportService(engine).preview(csv_files(data), AS_OF)


def test_partial_sources_preserve_unknown_instead_of_zero(engine):
    commit_sample(engine, omitted=("receipts",))
    report = analyze_arrival(Repository(engine, AS_OF).get_order_snapshot("PO-9001"))
    assert report["facts"]["received_qty"] is None
    assert report["facts"]["outstanding_qty"] is None
    assert report["facts"]["overdue_days"] is None
    assert report["anomalies"] == []
    assert any("不能把缺失数据当作零" in gap for gap in report["data_gaps"])


def test_explicitly_empty_receipt_table_means_zero(engine):
    data = sample_data()
    data["receipts"] = []
    commit_sample(engine, data)
    report = analyze_arrival(Repository(engine, AS_OF).get_order_snapshot("PO-9001"))
    assert report["facts"]["received_qty"] == 0
    assert report["facts"]["outstanding_qty"] == 1000
    assert report["facts"]["overdue_days"] == 2


def test_analysis_after_import_snapshot_makes_later_receipts_unknown(engine):
    commit_sample(engine)
    report = analyze_arrival(Repository(engine, date(2026, 10, 25)).get_order_snapshot("PO-9001"))
    assert report["facts"]["received_qty"] is None
    assert any("早于分析日" in gap for gap in report["data_gaps"])


def test_reimport_replaces_only_selected_imported_order(engine):
    before = Repository(engine, AS_OF).get_order_snapshot("PO-1001")
    commit_sample(engine)
    changed = sample_data()
    changed["receipts"][0].update(id="UPDATED-RECEIPT", qty=800)
    result = commit_sample(engine, changed)
    assert result["replaced_orders"] == 1 and result["new_orders"] == 0
    snapshot = Repository(engine, AS_OF).get_order_snapshot("PO-9001")
    assert [row["id"] for row in snapshot["receipts"]] == ["UPDATED-RECEIPT"]
    assert analyze_arrival(snapshot)["facts"]["outstanding_qty"] == 200
    assert Repository(engine, AS_OF).get_order_snapshot("PO-1001") == before
    init_database(engine, seed=True)
    assert analyze_arrival(Repository(engine, AS_OF).get_order_snapshot("PO-9001"))["facts"]["received_qty"] == 800


def test_preview_conflict_and_expiry(engine):
    commit_sample(engine)
    importer = ImportService(engine)
    first = importer.preview(csv_files(sample_data()), AS_OF)
    second = importer.preview(csv_files(sample_data()), AS_OF)
    importer.commit(first["preview_id"])
    with pytest.raises(ImportConflict, match="已变化"):
        importer.commit(second["preview_id"])
    third = importer.preview(csv_files(sample_data()), AS_OF)
    importer._previews[third["preview_id"]].expires_at = time.monotonic() - 1
    with pytest.raises(ImportConflict, match="失效"):
        importer.commit(third["preview_id"])


def test_demo_order_cannot_be_overwritten(engine):
    data = sample_data()
    data["orders"][0]["id"] = "PO-1001"
    data["shipments"][0]["order_id"] = "PO-1001"
    with pytest.raises(ImportConflict, match="不能覆盖"):
        ImportService(engine).preview(csv_files(data), AS_OF)


def test_foreign_shipments_and_supplier_names_cannot_be_overwritten(engine):
    commit_sample(engine)
    data = sample_data()
    data["orders"][0]["id"] = "PO-9002"
    data["shipments"][0]["order_id"] = "PO-9002"
    with pytest.raises(ImportConflict, match="属于其他订单"):
        ImportService(engine).preview(csv_files(data), AS_OF)
    data = sample_data()
    data["orders"][0]["supplier_id"] = "SUP-01"
    data["suppliers"][0]["id"] = "SUP-01"
    with pytest.raises(ImportConflict, match="不能修改"):
        ImportService(engine).preview(csv_files(data), AS_OF)


def test_database_write_failure_rolls_back_snapshot_replacement(engine, monkeypatch):
    commit_sample(engine)
    before = Repository(engine, AS_OF).get_order_snapshot("PO-9001")
    changed = sample_data()
    changed["receipts"][0]["qty"] = 900
    importer = ImportService(engine)
    preview = importer.preview(csv_files(changed), AS_OF)
    merge = Session.merge

    def fail_on_receipt(self, instance, **kwargs):
        if isinstance(instance, Receipt):
            raise RuntimeError("injected write failure")
        return merge(self, instance, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(Session, "merge", fail_on_receipt)
        with pytest.raises(RuntimeError):
            importer.commit(preview["preview_id"])
    assert Repository(engine, AS_OF).get_order_snapshot("PO-9001") == before


def test_csv_chinese_aliases_bom_and_gb18030():
    aliases = {("orders", "id"): "采购单号", ("orders", "supplier_id"): "供应商编号",
               ("orders", "qty"): "采购数量", ("suppliers", "id"): "供应商编号",
               ("suppliers", "name"): "供应商名称"}
    files = csv_files(sample_data(), aliases=aliases)
    encoded = [(name, body.decode("utf-8-sig").encode("gb18030")) for name, body in files]
    data, _, _ = parse_uploads(encoded)
    assert data == sample_data()


@pytest.mark.parametrize("files", [[], [("wrong.txt", b"abc")], [("orders.xlsx", b"not-a-zip")],
                                   [("orders.csv", b"id,qty\nx,1\n")], [("suppliers.csv", b"id,id\na,b\n")]])
def test_malformed_inputs_are_structured_errors(files):
    with pytest.raises(ImportValidationError):
        parse_uploads(files)


async def test_custom_business_id_date_and_import_source_in_agent_report(engine, tmp_path):
    data = sample_data()
    data["orders"][0]["id"] = "CG-2026-001"
    data["shipments"][0]["order_id"] = "CG-2026-001"
    commit_sample(engine, data)
    service = AgentService(engine, Settings(_env_file=None, agent_mode="demo", embedding_mode="local", index_dir=tmp_path))
    result = await service.run("分析CG-2026-001的到货情况", as_of=AS_OF)
    assert result["status"] == "completed"
    assert result["report"]["order_id"] == "CG-2026-001"
    assert result["report"]["facts"]["outstanding_qty"] == 400
    assert "用户导入数据" in result["markdown"]
    assert "所有数据均为项目模拟" not in result["markdown"]
    assert load_run(engine, result["run_id"])["report"]["data_source"]["kind"] == "imported"


async def test_case_preserving_imported_order_and_numeric_quantity_not_misrouted(engine, tmp_path):
    data = sample_data()
    data["orders"][0]["id"] = "po-9001"
    data["shipments"][0]["order_id"] = "po-9001"
    commit_sample(engine, data)
    numeric = sample_data()
    numeric["orders"][0]["id"] = "1000"
    numeric["shipments"][0]["order_id"] = "1000"
    for table in ("shipments", "eta_history", "logistics_events", "receipts"):
        for row in numeric[table]:
            row["id"] += "-numeric"
            if "shipment_id" in row:
                row["shipment_id"] += "-numeric"
    commit_sample(engine, numeric)
    service = AgentService(engine, Settings(_env_file=None, agent_mode="demo", embedding_mode="local", index_dir=tmp_path))
    first = await service.run("分析po9001的情况")
    assert first["report"]["order_id"] == "po-9001"
    follow = await service.run("订购数量是1000件吗？", first["session_id"])
    assert follow["report"]["order_id"] == "po-9001"
    explicit = await service.run("分析订单1000")
    assert explicit["report"]["order_id"] == "1000"


def test_case_only_collision_with_demo_is_rejected(engine):
    data = sample_data()
    data["orders"][0]["id"] = "po-1001"
    data["shipments"][0]["order_id"] = "po-1001"
    with pytest.raises(ImportConflict, match="大小写"):
        ImportService(engine).preview(csv_files(data), AS_OF)


def test_source_order_created_after_selected_date_warns(engine):
    preview = ImportService(engine).preview(csv_files(sample_data()), date(2026, 9, 30))
    assert any("创建时间晚于" in warning for warning in preview["warnings"])


def test_import_routes_preview_commit_template_and_analysis(tmp_path):
    settings = Settings(_env_file=None, database_url="sqlite:///:memory:", agent_mode="demo", embedding_mode="local", index_dir=tmp_path)
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/imports/template?format=xlsx").content.startswith(b"PK")
        assert client.get("/api/imports/template?format=csv").content.startswith(b"PK")
        files = [("files", (name, body, "text/csv")) for name, body in csv_files(sample_data())]
        response = client.post("/api/imports/preview", data={"as_of": "2026-10-24"}, files=files)
        assert response.status_code == 200, response.text
        token = response.json()["preview_id"]
        assert client.post("/api/imports/commit", json={"preview_id": token, "confirm_snapshot": False}).status_code == 422
        committed = client.post("/api/imports/commit", json={"preview_id": token, "confirm_snapshot": True})
        assert committed.status_code == 200, committed.text
        assert client.get("/health").json()["data_counts"]["imported"] == 1
        assert client.post("/api/imports/commit", json={"preview_id": token, "confirm_snapshot": True}).status_code == 409
        analysis = client.post("/api/chat", json={"message": "分析PO-9001", "as_of": "2026-10-24"}).json()
        assert analysis["report"]["facts"]["received_qty"] == 600
        later = client.post("/api/chat", json={"message": "分析PO-9001", "as_of": "2026-10-25"}).json()
        assert later["report"]["facts"]["received_qty"] is None
        bad = client.post("/api/imports/preview", data={"as_of": "2026-10-24"}, files=[("files", ("bad.xlsx", b"bad", "application/octet-stream"))])
        assert bad.status_code == 422 and bad.json()["detail"]["errors"]
