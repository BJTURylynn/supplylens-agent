"""Read-only order snapshots with an explicit Shanghai end-of-day cutoff."""

from __future__ import annotations

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from supplylens.db import (
    ETAHistory,
    LogisticsEvent,
    OrderOrigin,
    PurchaseOrder,
    Receipt,
    Shipment,
    Supplier,
    cutoff_after,
    iso_datetime,
)


class OrderNotFound(LookupError):
    pass


def evidence(identifier: str, source: str, description: str, fields: dict) -> dict:
    return {"id": identifier, "source": source, "description": description, "fields": fields}


class Repository:
    def __init__(self, engine: Engine, as_of: date):
        self.engine = engine
        self.as_of = as_of
        self.cutoff = cutoff_after(as_of)

    def mentioned_order_ids(self, message: str) -> list[str]:
        """Recognize exact stored identifiers, including non-PO enterprise codes."""
        import re

        with Session(self.engine) as session:
            ids = session.scalars(select(PurchaseOrder.id).where(PurchaseOrder.created_at < self.cutoff)).all()
        result = []
        for identifier in ids:
            pattern = re.escape(identifier)
            if re.fullmatch(r"PO-\d{4,}", identifier, re.IGNORECASE):
                pattern = "PO[-_\\s]?" + identifier[3:]
            if identifier.isdecimal() or len(identifier) < 4:
                pattern = r"(?:订单|采购单|单号)\s*[:：]?\s*" + pattern
            else:
                pattern = r"(?<![A-Za-z0-9_])" + pattern
            if re.search(pattern + r"(?![A-Za-z0-9_])", message, re.IGNORECASE):
                result.append(identifier)
        return result

    def source_counts(self) -> dict:
        with Session(self.engine) as session:
            total = session.scalar(select(func.count()).select_from(PurchaseOrder))
            imported = session.scalar(select(func.count()).select_from(OrderOrigin))
            return {"synthetic": total - imported, "imported": imported}

    def _source(self, origin: OrderOrigin | None) -> dict:
        if origin is None:
            return {"kind": "synthetic", "name": "内置模拟数据", "coverage": {"receipts": True}}
        coverage = dict(origin.coverage)
        if self.as_of > origin.as_of:
            coverage["receipts"] = False
        return {"kind": "imported", "name": origin.source_name, "batch_id": origin.batch_id,
                "snapshot_date": origin.as_of.isoformat(), "coverage": coverage}

    def list_orders(self, limit: int = 100) -> list[dict]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer between 1 and 1000")
        with Session(self.engine) as session:
            rows = session.execute(
                select(PurchaseOrder, Supplier.name).join(Supplier, PurchaseOrder.supplier_id == Supplier.id)
                .where(PurchaseOrder.created_at < self.cutoff).order_by(PurchaseOrder.id).limit(limit)
            ).all()
            ids = [order.id for order, _ in rows]
            totals = dict(session.execute(
                select(Shipment.order_id, func.sum(Receipt.qty)).join(Receipt, Receipt.shipment_id == Shipment.id)
                .where(Shipment.order_id.in_(ids), Shipment.shipped_at < self.cutoff, Receipt.received_at < self.cutoff)
                .group_by(Shipment.order_id)
            ).all()) if ids else {}
            origins = {row.order_id: row for row in session.scalars(select(OrderOrigin).where(OrderOrigin.order_id.in_(ids))).all()} if ids else {}
            return [{"order_id": order.id, "supplier_id": order.supplier_id, "supplier_name": supplier_name,
                     "material_code": order.material_code, "ordered_qty": order.qty,
                     "received_qty": totals.get(order.id, 0) if self._source(origins.get(order.id))["coverage"].get("receipts", False) else None,
                     "outstanding_qty": order.qty - totals.get(order.id, 0) if self._source(origins.get(order.id))["coverage"].get("receipts", False) else None,
                     "data_source": self._source(origins.get(order.id)),
                     "promised_date": order.promised_date.isoformat(), "as_of": self.as_of.isoformat()}
                    for order, supplier_name in rows]

    def get_order_snapshot(self, order_id: str) -> dict:
        with Session(self.engine) as session:
            pair = session.execute(
                select(PurchaseOrder, Supplier.name).join(Supplier, PurchaseOrder.supplier_id == Supplier.id)
                .where(PurchaseOrder.id == order_id, PurchaseOrder.created_at < self.cutoff)
            ).first()
            if pair is None:
                raise OrderNotFound(f"订单 {order_id} 不存在，或在 {self.as_of.isoformat()} 快照中尚不可见。")
            order, supplier_name = pair
            origin = session.get(OrderOrigin, order.id)
            source = self._source(origin)
            shipment_rows = session.scalars(select(Shipment).where(
                Shipment.order_id == order_id, Shipment.shipped_at < self.cutoff
            ).order_by(Shipment.id)).all()
            shipment_ids = [row.id for row in shipment_rows]
            eta_rows = session.scalars(select(ETAHistory).where(
                ETAHistory.shipment_id.in_(shipment_ids), ETAHistory.updated_at < self.cutoff
            ).order_by(ETAHistory.updated_at, ETAHistory.id)).all() if shipment_ids else []
            event_rows = session.scalars(select(LogisticsEvent).where(
                LogisticsEvent.shipment_id.in_(shipment_ids), LogisticsEvent.event_time < self.cutoff
            ).order_by(LogisticsEvent.event_time, LogisticsEvent.id)).all() if shipment_ids else []
            receipt_rows = session.scalars(select(Receipt).where(
                Receipt.shipment_id.in_(shipment_ids), Receipt.received_at < self.cutoff
            ).order_by(Receipt.received_at, Receipt.id)).all() if shipment_ids else []
            # Timestamp then immutable ID makes tied records deterministic in both engines.
            latest_eta = {row.shipment_id: row for row in eta_rows}
            latest_event = {row.shipment_id: row for row in event_rows}
            order_fields = {"id": order.id, "supplier_id": order.supplier_id, "supplier_name": supplier_name,
                            "material_code": order.material_code, "qty": order.qty,
                            "promised_date": order.promised_date.isoformat(), "created_at": iso_datetime(order.created_at),
                            "as_of": self.as_of.isoformat()}
            evidence_rows = [evidence(f"PO:{order.id}", "purchase_orders", "采购订单与承诺到货日", order_fields)]
            shipments = []
            for row in shipment_rows:
                eta = latest_eta.get(row.id)
                event_row = latest_event.get(row.id)
                shipment = {"id": row.id, "qty": row.qty, "shipped_at": iso_datetime(row.shipped_at),
                            "eta": eta.eta.isoformat() if eta else None,
                            "eta_updated_at": iso_datetime(eta.updated_at) if eta else None,
                            "eta_evidence_id": f"ETA:{eta.id}" if eta else None,
                            "latest_event": None}
                evidence_rows.append(evidence(f"SHIPMENT:{row.id}", "shipments", "已发运批次与数量",
                                              {"id": row.id, "order_id": order.id, "qty": row.qty,
                                               "shipped_at": iso_datetime(row.shipped_at)}))
                if eta:
                    evidence_rows.append(evidence(f"ETA:{eta.id}", "eta_history", "快照时已知的最新 ETA 版本",
                                                  {"id": eta.id, "shipment_id": row.id, "eta": eta.eta.isoformat(),
                                                   "updated_at": iso_datetime(eta.updated_at)}))
                if event_row:
                    shipment["latest_event"] = {"id": event_row.id, "status": event_row.status,
                                                "event_time": iso_datetime(event_row.event_time), "note": event_row.note}
                    evidence_rows.append(evidence(f"EVENT:{event_row.id}", "logistics_events", "该批次最新物流事件",
                                                  {"shipment_id": row.id, **shipment["latest_event"]}))
                shipments.append(shipment)
            receipts = [{"id": row.id, "shipment_id": row.shipment_id, "qty": row.qty,
                         "received_at": iso_datetime(row.received_at)} for row in receipt_rows]
            for row in receipts:
                evidence_rows.append(evidence(f"RECEIPT:{row['id']}", "receipts", "已登记收货记录", dict(row)))
            if origin:
                evidence_rows.append(evidence(f"IMPORT:{origin.batch_id}:{order.id}", "order_origins", "用户导入数据的来源与完整性声明", source))
            gaps = []
            if origin and self.as_of > origin.as_of:
                gaps.append(f"源文件快照截至 {origin.as_of.isoformat()}，早于分析日 {self.as_of.isoformat()}；后续收货状态未知。")
            return {"order_id": order.id, "supplier_id": order.supplier_id, "supplier_name": supplier_name,
                    "material_code": order.material_code, "ordered_qty": order.qty,
                    "promised_date": order.promised_date.isoformat(), "as_of": self.as_of.isoformat(),
                    "shipments": shipments, "receipts": receipts, "evidence": evidence_rows, "data_gaps": gaps,
                    "data_source": source}
