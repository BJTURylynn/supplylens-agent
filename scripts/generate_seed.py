"""Reproduce synthetic business records without calling a model or live service.

The independent acceptance oracle is hand authored in tests/fixtures/rule_cases.json;
this generator never computes or rewrites that oracle.
"""

from __future__ import annotations

import json
from pathlib import Path

AS_OF = "2026-10-24"


def build_seed() -> dict:
    data = {"synthetic": True, "as_of": AS_OF, "description": "教学用模拟数据；不代表任何真实车企或供应商。",
            "suppliers": [], "orders": [], "shipments": [], "eta_history": [], "logistics_events": [], "receipts": []}
    materials = ["MAT-SENSOR-01", "MAT-CONNECTOR-02", "MAT-HARNESS-03", "MAT-BRACKET-04", "MAT-RELAY-05"]
    for number in range(1, 11):
        data["suppliers"].append({"id": f"SUP-{number:02d}", "name": f"模拟供应商{number:02d}"})

    def order(number: int, qty: int, promise: int) -> str:
        identifier = f"PO-{number}"
        data["orders"].append({"id": identifier, "supplier_id": f"SUP-{(number - 1001) % 10 + 1:02d}",
                               "material_code": materials[(number - 1001) % len(materials)], "qty": qty,
                               "promised_date": f"2026-10-{promise:02d}", "created_at": "2026-10-01T09:00:00+08:00"})
        return identifier

    def shipment(number: int, suffix: str, qty: int, eta: int | None, status: str = "in_transit",
                 shipped: int = 20, event_day: int = 23, event_time: str = "10:00:00", note: str | None = None) -> str:
        identifier = f"SH-{number}-{suffix}"
        data["shipments"].append({"id": identifier, "order_id": f"PO-{number}", "qty": qty,
                                  "shipped_at": f"2026-10-{shipped:02d}T08:00:00+08:00"})
        if eta is not None:
            data["eta_history"].append({"id": f"ETA-{number}-{suffix}-1", "shipment_id": identifier,
                                        "eta": f"2026-10-{eta:02d}", "updated_at": f"2026-10-{shipped:02d}T09:00:00+08:00"})
        notes = {"in_transit": "承运商记录：在途运输中。", "held": "承运商记录：中转场滞留，原因待核实。",
                 "delayed": "承运商记录：运输延误，原因待核实。", "delivered": "承运商记录：已签收。"}
        data["logistics_events"].append({"id": f"EV-{number}-{suffix}-1", "shipment_id": identifier,
                                         "status": status, "event_time": f"2026-10-{event_day:02d}T{event_time}+08:00",
                                         "note": note if note is not None else notes.get(status, "模拟状态记录。")})
        return identifier

    def receipt(number: int, suffix: str, qty: int, day: int, serial: int = 1, clock: str = "15:00:00") -> None:
        data["receipts"].append({"id": f"RC-{number}-{suffix}-{serial}", "shipment_id": f"SH-{number}-{suffix}",
                                 "qty": qty, "received_at": f"2026-10-{day:02d}T{clock}+08:00"})

    order(1001, 1000, 22)
    shipment(1001, "A", 600, 22, "delivered", event_day=22)
    receipt(1001, "A", 600, 22)
    shipment(1001, "B", 400, 27, "held", event_day=24)

    order(1002, 800, 28)
    shipment(1002, "A", 800, 27)

    order(1003, 500, 23)  # No shipment coverage.

    order(1004, 600, 26)
    shipment(1004, "A", 600, 29)

    order(1005, 300, 26)
    shipment(1005, "A", 300, 26, "held")

    order(1006, 400, 21)
    shipment(1006, "A", 400, 23, "held", shipped=18, event_day=20)
    receipt(1006, "A", 400, 21)  # Old exception must not survive completed receipt.

    order(1007, 900, 26)
    shipment(1007, "A", 600, 29, "held", event_day=23)
    receipt(1007, "A", 250, 23)
    receipt(1007, "A", 350, 24, serial=2)
    shipment(1007, "B", 300, 26)

    order(1008, 700, 26)
    shipment(1008, "A", 400, 27)  # Remaining 300 has no shipment or ETA.

    order(1009, 500, 26)
    shipment(1009, "A", 500, None)

    order(1010, 200, 26)
    shipment(1010, "A", 200, 23)  # ETA has passed, promise has not.

    order(1011, 100, 25)
    shipment(1011, "A", 100, 24)
    data["eta_history"].append({"id": "ETA-1011-A-2", "shipment_id": "SH-1011-A", "eta": "2026-10-29",
                                "updated_at": "2026-10-25T00:00:00+08:00"})
    data["logistics_events"].append({"id": "EV-1011-A-2", "shipment_id": "SH-1011-A", "status": "held",
                                     "event_time": "2026-10-25T00:00:00+08:00", "note": "未来记录，不可出现在10月24日快照。"})
    receipt(1011, "A", 100, 25)

    order(1012, 250, 24)
    shipment(1012, "A", 250, 24, "held", shipped=24, event_day=24, event_time="23:59:59")
    receipt(1012, "A", 100, 24, clock="23:59:59")
    receipt(1012, "A", 150, 25, serial=2, clock="00:00:00")

    # Additional deterministic records broaden browsing; named cases above are the oracle.
    for number in range(1013, 1101):
        qty = 100 * (number % 8 + 1)
        scenario = number % 6
        promise = {0: 22, 1: 28, 2: 26, 3: 27, 4: 21, 5: 26}[scenario]
        order(number, qty, promise)
        if scenario == 0:
            shipment(number, "A", qty, 27, "delayed")
        elif scenario == 1:
            shipment(number, "A", qty, 27)
        elif scenario == 2:
            shipment(number, "A", qty, 29)
        elif scenario == 3:
            shipment(number, "A", qty, None)
        elif scenario == 4:
            shipment(number, "A", qty, 21, "delivered", event_day=21)
            receipt(number, "A", qty, 21)
        else:
            shipment(number, "A", qty // 2, 27, "held")
    return data


if __name__ == "__main__":
    target = Path(__file__).resolve().parents[1] / "data" / "seed.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(build_seed(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote 100 synthetic orders / 10 suppliers to {target}")
