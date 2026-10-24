"""Deterministic arrival rules. Models never calculate quantities or classify risk."""

from __future__ import annotations

from collections import defaultdict
from datetime import date

EXCEPTION_STATUSES = {"delayed", "held", "customs_hold", "exception"}
EXCEPTION_LABELS = {"delayed": "延误", "held": "滞留", "customs_hold": "清关滞留", "exception": "物流异常"}


def analyze_arrival(snapshot: dict) -> dict:
    """Analyze one immutable snapshot; missing information does not erase findings.

    ETA_DELAY_RISK applies only on or before the promised date. After that date
    the actual unmet promise is represented by OVERDUE_UNRECEIVED instead.
    """
    as_of = date.fromisoformat(snapshot["as_of"])
    promised = date.fromisoformat(snapshot["promised_date"])
    ordered = snapshot["ordered_qty"]
    received_by_shipment: dict[str, int] = defaultdict(int)
    for row in snapshot["receipts"]:
        received_by_shipment[row["shipment_id"]] += row["qty"]
    received = sum(received_by_shipment.values())
    outstanding = ordered - received
    if outstanding < 0:
        raise ValueError("received quantity exceeds ordered quantity")
    for shipment in snapshot["shipments"]:
        if received_by_shipment[shipment["id"]] > shipment["qty"]:
            raise ValueError(f"received quantity exceeds shipment {shipment['id']}")
    overdue_days = max((as_of - promised).days, 0) if outstanding > 0 else 0
    evidence_rows = [dict(row) for row in snapshot["evidence"]]
    evidence_ids = {row["id"] for row in evidence_rows}
    po_id = f"PO:{snapshot['order_id']}"
    fact_id = f"CALC:{snapshot['order_id']}:{snapshot['as_of']}"
    facts = {"supplier_name": snapshot["supplier_name"], "material_code": snapshot["material_code"],
             "ordered_qty": ordered, "received_qty": received, "outstanding_qty": outstanding,
             "promised_date": snapshot["promised_date"], "overdue_days": overdue_days}
    evidence_rows.append({"id": fact_id, "source": "arrival_rules", "description": "固定快照下的收货汇总与逾期计算",
                          "fields": {"order_id": snapshot["order_id"], "as_of": snapshot["as_of"], **facts,
                                     "formula": "received=sum(receipts.qty); outstanding=ordered-received; overdue=max(as_of-promised,0) if outstanding>0 else 0",
                                     "input_evidence_ids": [po_id] + [f"RECEIPT:{row['id']}" for row in snapshot["receipts"]]}})
    evidence_ids.add(fact_id)
    anomalies = []
    gaps = list(snapshot.get("data_gaps", []))

    def add(code: str, title: str, detail: str, shipment_id: str | None, refs: list[str]) -> None:
        anomalies.append({"code": code, "title": title, "detail": detail, "shipment_id": shipment_id,
                          "evidence_ids": [ref for ref in dict.fromkeys(refs) if ref in evidence_ids]})

    if outstanding > 0:
        if overdue_days:
            add("OVERDUE_UNRECEIVED", "逾期未收齐",
                f"承诺到货日为 {promised.isoformat()}；截至 {as_of.isoformat()} 尚有 {outstanding} 件未收货，逾期 {overdue_days} 天。",
                None, [po_id, fact_id])
        covered = sum(row["qty"] for row in snapshot["shipments"])
        if covered < ordered:
            gaps.append(f"尚有 {ordered - covered} 件未被快照内发运记录覆盖，无法判断这部分的 ETA。")
        for shipment in snapshot["shipments"]:
            shipment_id = shipment["id"]
            remaining = shipment["qty"] - received_by_shipment[shipment_id]
            if remaining <= 0:
                continue
            ship_ref = f"SHIPMENT:{shipment_id}"
            eta = date.fromisoformat(shipment["eta"]) if shipment.get("eta") else None
            eta_ref = shipment.get("eta_evidence_id")
            if eta is None:
                gaps.append(f"发运单 {shipment_id} 尚欠 {remaining} 件，缺少 ETA。")
            elif eta < as_of:
                gaps.append(f"发运单 {shipment_id} 的 ETA {eta.isoformat()} 已过期且尚未收齐，需更新 ETA。")
            elif as_of <= promised and eta > promised:
                add("ETA_DELAY_RISK", "预计到货延期风险",
                    f"发运单 {shipment_id} 尚欠 {remaining} 件，ETA {eta.isoformat()} 晚于承诺日 {promised.isoformat()}。",
                    shipment_id, [po_id, ship_ref, eta_ref, fact_id])
            event = shipment.get("latest_event")
            if event is None:
                gaps.append(f"发运单 {shipment_id} 缺少物流事件，无法核实当前运输状态。")
            elif event["status"] in EXCEPTION_STATUSES:
                label = EXCEPTION_LABELS[event["status"]]
                add("LOGISTICS_EXCEPTION", "物流显式异常",
                    f"发运单 {shipment_id} 最新物流状态为{label}；记录备注：{event['note'] or '未填写'}。备注仅为记录信息，根因仍需核实。",
                    shipment_id, [ship_ref, f"EVENT:{event['id']}", fact_id])
            elif event["status"] in {"delivered", "arrived"}:
                gaps.append(f"发运单 {shipment_id} 物流显示到达/签收，但收货记录尚欠 {remaining} 件，需核对收货登记。")
    return {"order_id": snapshot["order_id"], "as_of": snapshot["as_of"], "facts": facts,
            "anomalies": anomalies, "data_gaps": list(dict.fromkeys(gaps)), "evidence": evidence_rows}
