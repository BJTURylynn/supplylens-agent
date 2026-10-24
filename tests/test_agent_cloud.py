"""Offline cloud-protocol tests: real graph/tools/storage, scripted chat replies.

These validate orchestration and provider-shaped messages, not live-model quality.
"""

import asyncio
import inspect
import json
from datetime import date

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from supplylens import reports
from supplylens.agent import AgentService
from supplylens.config import Settings
from supplylens.db import import_seed, init_database, load_run, make_engine

SOP_QUERY = "逾期未收货供应商跟进"
SECRET = "unit-test-provider-secret-never-display"


class ScriptedChatModel:
    """Only the external chat transport is replaced; tool execution is real."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.bound_tools = []

    def bind_tools(self, tools):
        self.bound_tools.append({tool.name for tool in tools})
        return self

    async def ainvoke(self, messages):
        self.calls.append(list(messages))
        if not self.replies:
            raise AssertionError("Unexpected additional model round")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if callable(reply):
            reply = reply(messages)
        if inspect.isawaitable(reply):
            reply = await reply
        return reply


def tool_message(*calls):
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": arguments, "id": call_id} for name, arguments, call_id in calls],
        usage_metadata={"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
    )


def normal_tool_message():
    return tool_message(
        ("analyze_arrival", {"order_id": "PO-1001"}, "analysis-call"),
        ("search_sop", {"query": SOP_QUERY}, "sop-call"),
    )


def final_message(payload):
    return AIMessage(
        content=json.dumps(payload, ensure_ascii=False),
        usage_metadata={"input_tokens": 13, "output_tokens": 5, "total_tokens": 18},
    )


def valid_payload(ref):
    return {"actions": [{"text": reports.source_lines(ref)[0], "sop_ids": [ref["id"]]}], "notes": []}


def invalid_reference_message():
    return final_message({
        "actions": [{"text": "应启动专用虚构流程", "sop_ids": ["sop:invented-reference"]}], "notes": [],
    })


@pytest.fixture
def cloud_environment(tmp_path):
    database_url = f"sqlite:///{(tmp_path / 'cloud-test.db').as_posix()}"
    engine = make_engine(database_url)
    init_database(engine, seed=False)
    import_seed(engine, {
        "suppliers": [{"id": "SUP-A", "name": "模拟测试供应商"}],
        "orders": [
            {"id": "PO-1001", "supplier_id": "SUP-A", "material_code": "MAT-A", "qty": 10,
             "promised_date": "2026-10-20", "created_at": "2026-10-01T08:00:00+08:00"},
            {"id": "PO-1002", "supplier_id": "SUP-A", "material_code": "MAT-B", "qty": 999,
             "promised_date": "2026-10-28", "created_at": "2026-10-01T08:00:00+08:00"},
        ],
        "shipments": [{"id": "SHIP-A", "order_id": "PO-1001", "qty": 10,
                       "shipped_at": "2026-10-18T08:00:00+08:00"}],
        "eta_history": [{"id": "ETA-A", "shipment_id": "SHIP-A", "eta": "2026-10-26",
                         "updated_at": "2026-10-22T09:00:00+08:00"}],
        "logistics_events": [{"id": "EVENT-A", "shipment_id": "SHIP-A", "status": "delayed",
                              "event_time": "2026-10-22T10:00:00+08:00", "note": "模拟异常待核实"}],
        "receipts": [{"id": "RECEIPT-A", "shipment_id": "SHIP-A", "qty": 4,
                      "received_at": "2026-10-23T10:00:00+08:00"}],
    })
    settings = Settings(
        _env_file=None, database_url=database_url, agent_mode="cloud", embedding_mode="local",
        model_api_key=SECRET, index_dir=tmp_path / "index", as_of_date=date(2026, 10, 24),
        request_timeout_seconds=10, model_timeout_seconds=3,
    )
    yield engine, settings
    engine.dispose()


def service_with_model(cloud_environment, replies, **overrides):
    engine, settings = cloud_environment
    if overrides:
        settings = settings.model_copy(update=overrides)
    model = ScriptedChatModel(replies)
    service = AgentService(engine, settings, model_factory=lambda: model)
    return service, model


async def test_trial_quota_notice_stops_graph_and_explains_limitation(cloud_environment):
    service, model = service_with_model(cloud_environment, [AIMessage(content=(
        "Sorry, to prevent abuse of free resources, accounts that have not been recharged can only try 10 times."
    ))])
    result = await service.run("分析 PO-1001")
    assert result["status"] == "partial"
    assert "试用次数已用完" in result["answer"]
    assert result["report"] is None
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_cloud_model_selected_tools_execute_and_report_has_valid_evidence(cloud_environment):
    def finish(messages):
        results = {message.tool_call_id: json.loads(message.content)
                   for message in messages if isinstance(message, ToolMessage)}
        assert results["analysis-call"]["facts"]["outstanding_qty"] == 6
        assert results["sop-call"]
        return final_message(valid_payload(results["sop-call"][0]))

    service, model = service_with_model(cloud_environment, [normal_tool_message(), finish])
    result = await service.run("分析 PO-1001，并写供应商跟进建议")
    assert result["status"] == "completed"
    assert result["mode"] == "cloud"
    assert model.bound_tools == [{"get_order_snapshot", "analyze_arrival", "search_sop"}]
    assert [step["name"] for step in result["steps"]] == ["get_order_snapshot", "analyze_arrival", "search_sop"]
    assert all(step["status"] == "ok" for step in result["steps"])
    report = result["report"]
    assert report["order_id"] == "PO-1001"
    assert report["facts"]["ordered_qty"] == 10
    assert report["facts"]["received_qty"] == 4
    assert report["facts"]["outstanding_qty"] == 6
    evidence_ids = {evidence["id"] for evidence in report["evidence"]}
    assert report["anomalies"]
    for anomaly in report["anomalies"]:
        assert anomaly["evidence_ids"]
        assert set(anomaly["evidence_ids"]).issubset(evidence_ids)
    sop_ids = {ref["id"] for ref in report["sop_refs"]}
    assert report["actions"]
    assert set(report["actions"][0]["sop_ids"]).issubset(sop_ids)
    assert report["followup_draft"]
    assert result["metrics"]["tool_calls"] == 3
    assert result["metrics"]["model_rounds"] == 2
    assert result["metrics"]["input_tokens"] == 24
    assert result["metrics"]["output_tokens"] == 12
    assert load_run(cloud_environment[0], result["run_id"]) == result


@pytest.mark.asyncio
async def test_invalid_reference_gets_exactly_one_correction_round(cloud_environment):
    service, model = service_with_model(cloud_environment, [normal_tool_message(), invalid_reference_message()])
    ref = service.index.search(SOP_QUERY)[0]
    model.replies.append(final_message(valid_payload(ref)))
    result = await service.run("分析 PO-1001")
    assert result["status"] == "completed"
    assert len(model.calls) == 3
    assert result["metrics"]["model_rounds"] == 3
    assert "修正" in model.calls[-1][-1].content
    assert ref["id"] in model.calls[-1][-1].content
    assert result["report"]["actions"] == valid_payload(ref)["actions"]
    assert "sop:invented-reference" not in json.dumps(result, ensure_ascii=False)


@pytest.mark.asyncio
async def test_two_invalid_references_return_partial_with_source_fallback(cloud_environment):
    service, model = service_with_model(cloud_environment, [
        normal_tool_message(), invalid_reference_message(), invalid_reference_message(),
    ])
    result = await service.run("分析 PO-1001")
    assert result["status"] == "partial"
    assert result["report"]["facts"]["outstanding_qty"] == 6
    assert len(model.calls) == 3
    assert not model.replies
    assert any("引用校验" in note for note in result["report"]["notes"])
    assert result["report"]["actions"]
    valid_ids = {ref["id"] for ref in result["report"]["sop_refs"]}
    for action in result["report"]["actions"]:
        assert set(action["sop_ids"]).issubset(valid_ids)
    serialized = json.dumps(result, ensure_ascii=False)
    assert "sop:invented-reference" not in serialized
    assert "专用虚构流程" not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [{}, {"order_id": ""}, {"order_id": None},
                                       {"order_id": 1001}, {"order_id": "PO-1002"}])
async def test_illegal_analysis_arguments_cannot_switch_order(cloud_environment, monkeypatch, arguments):
    service, _ = service_with_model(cloud_environment, [
        tool_message(("analyze_arrival", arguments, "bad-analysis")),
        final_message({"actions": [], "notes": []}),
    ])
    calls = []
    get_snapshot = service.repo.get_order_snapshot

    def spy(order_id):
        calls.append(order_id)
        return get_snapshot(order_id)

    monkeypatch.setattr(service.repo, "get_order_snapshot", spy)
    result = await service.run("分析 PO-1001")
    assert result["status"] == "partial"
    assert calls and set(calls) == {"PO-1001"}
    assert any(step["name"] == "analyze_arrival" and step["status"] == "error" for step in result["steps"])
    assert result["report"] is None or result["report"]["order_id"] == "PO-1001"
    if result["report"]:
        assert result["report"]["facts"]["ordered_qty"] == 10


@pytest.mark.asyncio
async def test_early_final_answer_cannot_skip_authoritative_analysis(cloud_environment):
    service, model = service_with_model(cloud_environment, [final_message({"actions": [], "notes": []})])
    result = await service.run("分析 PO-1001")
    assert result["status"] == "completed"
    assert len(model.calls) == 1
    assert result["report"]["facts"]["outstanding_qty"] == 6
    assert {step["name"] for step in result["steps"]} == {"get_order_snapshot", "analyze_arrival", "search_sop"}


@pytest.mark.asyncio
async def test_tool_call_budget_stops_before_extra_execution(cloud_environment):
    service, model = service_with_model(cloud_environment, [normal_tool_message()], max_tool_calls=1)
    result = await service.run("分析 PO-1001")
    assert result["status"] == "partial"
    assert result["report"] is None
    assert len(model.calls) == 1
    assert result["metrics"]["tool_calls"] == 1
    assert [step["name"] for step in result["steps"]] == ["get_order_snapshot"]
    assert "上限" in result["answer"]


@pytest.mark.asyncio
async def test_model_round_budget_ends_with_preserved_analysis(cloud_environment):
    service, model = service_with_model(cloud_environment, [normal_tool_message()], max_model_rounds=1)
    result = await service.run("分析 PO-1001")
    assert result["status"] == "partial"
    assert result["report"]["facts"]["outstanding_qty"] == 6
    assert len(model.calls) == 1
    assert result["metrics"]["model_rounds"] == 1
    assert any("轮数" in note for note in result["report"]["notes"])


@pytest.mark.asyncio
async def test_model_timeout_ends_and_preserves_trace(cloud_environment):
    cancelled = []

    async def slow_reply(_messages):
        try:
            await asyncio.sleep(2)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        return final_message({"actions": [], "notes": []})

    service, model = service_with_model(cloud_environment, [slow_reply], model_timeout_seconds=0.02)
    result = await service.run("分析 PO-1001")
    assert result["status"] == "partial"
    assert result["report"] is None
    assert len(model.calls) == 1
    assert cancelled == [True]
    assert result["steps"][0]["status"] == "ok"
    assert "时间" in result["answer"]
    assert load_run(cloud_environment[0], result["run_id"])["status"] == "partial"


@pytest.mark.asyncio
async def test_provider_exception_does_not_expose_api_key(cloud_environment):
    service, _ = service_with_model(cloud_environment, [RuntimeError(f"request failed Authorization: Bearer {SECRET}")])
    result = await service.run("分析 PO-1001")
    assert result["status"] == "partial"
    assert "RuntimeError" in result["answer"]
    assert SECRET not in json.dumps(result, ensure_ascii=False)
    assert SECRET not in json.dumps(load_run(cloud_environment[0], result["run_id"]), ensure_ascii=False)


@pytest.mark.asyncio
async def test_tool_exception_does_not_expose_api_key(cloud_environment, monkeypatch):
    service, _ = service_with_model(cloud_environment, [
        tool_message(("analyze_arrival", {"order_id": "PO-1001"}, "analysis-call")),
        final_message({"actions": [], "notes": []}),
    ])
    def fail_analysis(snapshot):
        raise RuntimeError(f"API key in upstream exception: {SECRET}")

    monkeypatch.setattr("supplylens.agent.calculate_arrival", fail_analysis)
    result = await service.run("分析 PO-1001")
    assert result["status"] == "partial"
    assert result["steps"][1]["status"] == "error"
    assert "RuntimeError" in result["steps"][1]["result"]["error"]
    assert SECRET not in json.dumps(result, ensure_ascii=False)
    assert SECRET not in json.dumps(load_run(cloud_environment[0], result["run_id"]), ensure_ascii=False)
