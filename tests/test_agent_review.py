"""Independent regression cases for session targeting and report boundaries.

Async cases are run with the project suite by the integrator. Some Windows
sandbox profiles block asyncio's socketpair; this is unrelated to these tests.
"""

import json
import time

import pytest

from supplylens.agent import AgentService, BudgetExceeded, Execution
from supplylens.config import Settings
from supplylens.db import init_database, load_run, make_engine
from supplylens.reports import validate_narrative
from supplylens.schemas import Action, Narrative

SOP_REFS = [{"id": "arrival-followup#check", "document": "arrival-followup.md", "title": "收货核实",
             "text": "核对未收批次的物流信息与收货登记。", "score": 1.0, "source_type": "sop"}]


class ReviewIndex:
    mode = "local"

    def search(self, query, top_k=3):
        return SOP_REFS[:top_k]


@pytest.fixture
def review_service():
    engine = make_engine("sqlite:///:memory:")
    init_database(engine)
    settings = Settings(_env_file=None, agent_mode="demo", embedding_mode="local", model_api_key="")
    service = AgentService(engine, settings, index=ReviewIndex())
    yield service
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["分析PO-1004", "PO-1004为什么延迟", "请分析PO-1004为什么延迟"])
async def test_chinese_adjacent_order_replaces_previous_session_target(review_service, message):
    first = await review_service.run("分析 PO-1001")
    next_run = await review_service.run(message, first["session_id"])
    assert next_run["report"] is not None
    assert next_run["report"]["order_id"] == "PO-1004"
    assert next_run["report"]["facts"]["ordered_qty"] == 600


@pytest.mark.asyncio
async def test_nonexistent_explicit_order_cannot_reactivate_previous_order(review_service):
    first = await review_service.run("分析 PO-1001")
    missing = await review_service.run("分析 PO-9999", first["session_id"])
    assert missing["status"] == "failed"
    assert missing["report"] is None
    followup = await review_service.run("继续分析这个订单", first["session_id"])
    assert followup["report"] is None
    assert followup["status"] in {"needs_input", "failed"}


@pytest.mark.asyncio
async def test_cloud_tool_argument_cannot_switch_authoritative_order(review_service):
    ctx = Execution(order_id="PO-1001", message="分析订单", followup=False, deadline=time.monotonic() + 20)
    result = await review_service._dispatch(ctx, review_service._tools(ctx), "analyze_arrival", {"order_id": "PO-1004"})
    assert "error" in result
    assert ctx.analysis is None
    assert len(ctx.steps) == 1
    assert ctx.steps[0]["status"] == "error"


@pytest.mark.asyncio
async def test_tool_budget_includes_preparation_and_blocks_extra_execution(review_service):
    review_service.settings.max_tool_calls = 1
    ctx = Execution(order_id="PO-1001", message="分析订单", followup=False, deadline=time.monotonic() + 20)
    tools = review_service._tools(ctx)
    await review_service._dispatch(ctx, tools, "get_order_snapshot", {"order_id": "PO-1001"})
    with pytest.raises(BudgetExceeded):
        await review_service._dispatch(ctx, tools, "analyze_arrival", {"order_id": "PO-1001"})
    assert len(ctx.steps) == 1
    assert ctx.snapshot["order_id"] == "PO-1001"
    assert ctx.analysis is None


@pytest.mark.asyncio
async def test_upstream_secret_is_not_exposed_in_failure_or_persisted_trace(review_service):
    secret = "review-only-secret-token"

    class FailingIndex:
        mode = "local"

        def search(self, query, top_k=3):
            raise RuntimeError(f"Authorization: Bearer {secret}")

    review_service.index = FailingIndex()
    result = await review_service.run("分析 PO-1001")
    saved = load_run(review_service.engine, result["run_id"])
    assert result["status"] == "partial"
    assert result["report"]["order_id"] == "PO-1001"
    assert any(step["status"] == "error" for step in saved["steps"])
    assert secret not in json.dumps(result)
    assert secret not in json.dumps(saved)


def test_unsupported_qualitative_business_claim_is_rejected():
    narrative = Narrative(actions=[], notes=["供应商承担全部责任，工厂已停产。"])
    with pytest.raises(ValueError):
        validate_narrative(narrative, SOP_REFS)


def test_real_reference_id_does_not_authorize_unrelated_instruction():
    narrative = Narrative(actions=[Action(text="立即销毁这批零部件。", sop_ids=[SOP_REFS[0]["id"]])], notes=[])
    with pytest.raises(ValueError):
        validate_narrative(narrative, SOP_REFS)


def test_valid_procedural_source_sentence_is_accepted():
    narrative = Narrative(actions=[Action(text=SOP_REFS[0]["text"], sop_ids=[SOP_REFS[0]["id"]])], notes=[])
    validate_narrative(narrative, SOP_REFS)
