"""Independent hand-authored acceptance cases; this module is never imported by the agent."""
import asyncio
import json
import math
import platform
import statistics
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import patch

from supplylens.agent import AgentService
from supplylens.config import Settings
from supplylens.db import init_database, make_engine


class EmptyIndex:
    mode = "local"

    def search(self, query, top_k=3):
        return []


class BrokenIndex(EmptyIndex):
    def search(self, query, top_k=3):
        raise ConnectionError("injected retrieval failure")


class TimeoutModel:
    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages):
        await asyncio.sleep(10)


def check_expected(result: dict, expected: dict) -> list[str]:
    failures = []
    report = result.get("report")
    for key in ["status", "session_reset"]:
        if key in expected and result.get(key) != expected[key]:
            failures.append(f"{key}: expected {expected[key]!r}, got {result.get(key)!r}")
    if expected.get("no_report"):
        if report is not None:
            failures.append("expected no report")
    elif any(key in expected for key in ["facts", "order_id", "codes", "sop_count", "gap_count", "draft"]):
        if report is None:
            return failures + ["expected a verified report"]
        if "order_id" in expected and report["order_id"] != expected["order_id"]:
            failures.append("wrong order_id")
        for name, value in expected.get("facts", {}).items():
            if report["facts"].get(name) != value:
                failures.append(f"fact {name}: expected {value}, got {report['facts'].get(name)}")
        if "codes" in expected:
            actual = sorted({a["code"] for a in report["anomalies"]})
            if actual != sorted(expected["codes"]):
                failures.append(f"anomalies: expected {expected['codes']}, got {actual}")
        for key, field in [("sop_count", "sop_refs"), ("gap_count", "data_gaps")]:
            if key in expected and len(report[field]) != expected[key]:
                failures.append(f"{key}: expected {expected[key]}, got {len(report[field])}")
        if expected.get("draft") and not report.get("followup_draft"):
            failures.append("missing followup draft")
    for key, field in [("markdown_contains", "markdown"), ("answer_contains", "answer")]:
        if key in expected and expected[key] not in result.get(field, ""):
            failures.append(f"missing {key}: {expected[key]}")
    output = result.get("markdown", "") + result.get("answer", "")
    for forbidden in expected.get("forbidden", []):
        if forbidden in output:
            failures.append(f"unsupported output: {forbidden}")
    if report:
        evidence_ids = {e["id"] for e in report["evidence"]}
        sop_ids = {r["id"] for r in report["sop_refs"]}
        if any(not set(a.get("evidence_ids", [])).issubset(evidence_ids) for a in report["anomalies"]):
            failures.append("invalid business evidence reference")
        if any(not set(a["sop_ids"]).issubset(sop_ids) for a in report["actions"]):
            failures.append("invalid SOP reference")
    return failures


async def evaluate_cases(settings: Settings, mode: str, case_path: Path, output: Path) -> dict:
    cases = json.loads(case_path.read_text(encoding="utf-8"))
    if mode == "cloud" and not settings.model_api_key.strip():
        raise ValueError("真实云端评测需要 MODEL_API_KEY；未执行，也不会生成冒充云端的成绩。")
    results = []
    with tempfile.TemporaryDirectory(prefix="supplylens-eval-") as temp:
        config = settings.model_copy(update={
            "agent_mode": mode, "embedding_mode": "cloud" if mode == "cloud" else "local",
            "database_url": "sqlite:///:memory:", "as_of_date": date.fromisoformat(cases["as_of"]),
            "index_dir": Path(temp) / "index",
        })
        engine = make_engine(config.database_url)
        init_database(engine)
        try:
            for case in cases["cases"]:
                fault = case.get("fault")
                service = AgentService(engine, config)
                cleanup = None
                if fault == "missing_sop":
                    service.index = EmptyIndex()
                elif fault == "retrieval_failure":
                    service.index = BrokenIndex()
                elif fault == "calculation_failure":
                    cleanup = patch("supplylens.agent.calculate_arrival", side_effect=RuntimeError("injected calculation failure"))
                    cleanup.start()
                elif fault == "request_timeout":
                    service.settings = config.model_copy(update={"request_timeout_seconds": .08})
                    original_dispatch = service._dispatch

                    async def slow_dispatch(ctx, tools, name, arguments, original=original_dispatch):
                        if name == "analyze_arrival":
                            await asyncio.sleep(1)
                        return await original(ctx, tools, name, arguments)

                    service._dispatch = slow_dispatch
                elif fault == "model_timeout":
                    service.settings = config.model_copy(update={"agent_mode": "cloud", "model_timeout_seconds": .02})
                    service.model_factory = TimeoutModel
                sid = case.get("session_id")
                turns = []
                try:
                    for message in case["turns"]:
                        result = await service.run(message, sid)
                        turns.append(result)
                        sid = result["session_id"]
                    errors = check_expected(result, case["expected"])
                    results.append({
                        "id": case["id"], "name": case["name"], "passed": not errors, "errors": errors,
                        "fault_injection": fault, "live_cloud_model": mode == "cloud" and fault != "model_timeout",
                        "metrics": result["metrics"],
                        "turn_metrics": [turn["metrics"] for turn in turns],
                        "observed": {"status": result["status"], "answer": result["answer"],
                                     "order_id": (result.get("report") or {}).get("order_id"),
                                     "facts": (result.get("report") or {}).get("facts"),
                                     "anomaly_codes": [a["code"] for a in (result.get("report") or {}).get("anomalies", [])]},
                    })
                finally:
                    if cleanup is not None:
                        cleanup.stop()
        finally:
            engine.dispose()
    latencies = sorted(r["metrics"]["elapsed_ms"] for r in results)
    passed = sum(r["passed"] for r in results)
    live = [r for r in results if r["live_cloud_model"]]
    summary = {
        "mode": mode, "total": len(results), "passed": passed, "failed": len(results) - passed,
        "pass_rate": round(passed / len(results), 4) if results else None,
        "fault_injection_cases": sum(bool(r["fault_injection"]) for r in results),
        "live_cloud_cases": len(live),
        "p50_last_turn_ms": round(statistics.median(latencies), 2) if latencies else None,
        "p95_last_turn_ms": latencies[max(0, math.ceil(len(latencies) * .95) - 1)] if latencies else None,
        "input_tokens": sum(t["input_tokens"] or 0 for r in results for t in r["turn_metrics"]) if mode == "cloud" else None,
        "output_tokens": sum(t["output_tokens"] or 0 for r in results for t in r["turn_metrics"]) if mode == "cloud" else None,
        "interpretation": "固定样例的工作流/故障处理验收，含故障注入；不是开放域准确率。"
                          + ("未调用真实聊天或语义向量模型。" if mode == "demo" else "真实云模型样例与故障注入逐条标记。"),
    }
    artifact = {"generated_at": datetime.now(UTC).isoformat(), "python": platform.python_version(),
                "as_of": cases["as_of"], "model": settings.model_name if mode == "cloud" else None,
                "embedding_mode": "cloud" if mode == "cloud" else "local", "summary": summary, "cases": results}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return artifact
