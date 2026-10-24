"""Acceptance cases are manually specified, never generated from production rules."""

import json
from copy import deepcopy
from datetime import date
from pathlib import Path

import pytest

from supplylens.db import init_database, make_engine
from supplylens.repository import Repository
from supplylens.rules import analyze_arrival

ORACLE = json.loads((Path(__file__).parent / "fixtures" / "rule_cases.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def repository():
    engine = make_engine("sqlite:///:memory:")
    init_database(engine)
    yield Repository(engine, date.fromisoformat(ORACLE["as_of"]))
    engine.dispose()


@pytest.mark.parametrize("case", ORACLE["cases"], ids=lambda case: case["order_id"])
def test_manual_business_oracle(repository, case):
    analysis = analyze_arrival(repository.get_order_snapshot(case["order_id"]))
    for key in ("ordered_qty", "received_qty", "outstanding_qty", "overdue_days"):
        assert analysis["facts"][key] == case[key], (case["name"], key)
    assert sorted({row["code"] for row in analysis["anomalies"]}) == sorted(case["anomaly_codes"])
    assert len(analysis["anomalies"]) == case["anomaly_count"]
    assert len(analysis["data_gaps"]) == case["gap_count"]
    for text in case["gap_contains"]:
        assert text in " ".join(analysis["data_gaps"])


def test_evidence_is_unique_traceable_and_stable(repository):
    for summary in repository.list_orders():
        snapshot = repository.get_order_snapshot(summary["order_id"])
        analysis = analyze_arrival(snapshot)
        assert analysis == analyze_arrival(repository.get_order_snapshot(summary["order_id"]))
        evidence_ids = [row["id"] for row in analysis["evidence"]]
        assert len(evidence_ids) == len(set(evidence_ids))
        for anomaly in analysis["anomalies"]:
            assert anomaly["evidence_ids"]
            assert set(anomaly["evidence_ids"]).issubset(evidence_ids)
        for row in analysis["evidence"]:
            if row["source"] == "arrival_rules":
                assert set(row["fields"]["input_evidence_ids"]).issubset(evidence_ids)
        assert summary["received_qty"] == analysis["facts"]["received_qty"]


def test_analysis_does_not_mutate_snapshot(repository):
    snapshot = repository.get_order_snapshot("PO-1001")
    original = deepcopy(snapshot)
    analyze_arrival(snapshot)
    assert snapshot == original


def test_completed_batch_exception_not_other_batchs_current_status(repository):
    analysis = analyze_arrival(repository.get_order_snapshot("PO-1007"))
    assert not analysis["anomalies"]
    assert analysis["facts"]["outstanding_qty"] == 300


def test_logistics_event_on_one_batch_not_masked_by_other_batch(repository):
    snapshot = repository.get_order_snapshot("PO-1001")
    snapshot["shipments"][0]["latest_event"]["event_time"] = "2026-10-24T23:59:59+08:00"
    anomalies = analyze_arrival(snapshot)["anomalies"]
    assert any(row["code"] == "LOGISTICS_EXCEPTION" and row["shipment_id"] == "SH-1001-B" for row in anomalies)


def test_future_eta_risk_is_replaced_by_actual_overdue(repository):
    snapshot = repository.get_order_snapshot("PO-1004")
    snapshot["as_of"] = "2026-10-27"
    analysis = analyze_arrival(snapshot)
    assert {row["code"] for row in analysis["anomalies"]} == {"OVERDUE_UNRECEIVED"}
    assert analysis["facts"]["overdue_days"] == 1


def test_signed_delivery_without_receipt_is_incomplete_data(repository):
    snapshot = repository.get_order_snapshot("PO-1002")
    snapshot["shipments"][0]["latest_event"]["status"] = "delivered"
    result = analyze_arrival(snapshot)
    assert result["facts"]["received_qty"] == 0
    assert any("核对收货登记" in gap for gap in result["data_gaps"])


def test_missing_event_and_known_eta_risk_can_coexist(repository):
    snapshot = repository.get_order_snapshot("PO-1004")
    snapshot["shipments"][0]["latest_event"] = None
    result = analyze_arrival(snapshot)
    assert [row["code"] for row in result["anomalies"]] == ["ETA_DELAY_RISK"]
    assert any("缺少物流事件" in gap for gap in result["data_gaps"])


def test_invalid_snapshot_over_receipt_is_not_silently_clamped(repository):
    snapshot = repository.get_order_snapshot("PO-1001")
    snapshot["receipts"][0]["qty"] = 1100
    with pytest.raises(ValueError, match="exceeds"):
        analyze_arrival(snapshot)
