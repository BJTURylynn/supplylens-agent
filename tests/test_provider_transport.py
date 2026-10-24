"""Exercise the real ChatOpenAI SDK over an entirely in-process HTTP transport.

This is an adapter contract test, not a live DashScope or model-quality test.
"""

import json
from datetime import date

import httpx
import pytest
from langchain_openai import ChatOpenAI

from supplylens.agent import AgentService
from supplylens.config import Settings
from supplylens.db import init_database, load_run, make_engine


@pytest.mark.asyncio
async def test_real_chatopenai_adapter_tool_protocol_and_usage(tmp_path):
    dummy_token = "dummy-sdk-transport-token-never-display"
    base_url = "https://provider.example/compatible-mode/v1"
    requests = []
    transport_errors = []
    provider_tool_calls = [
        {"id": "call-analysis", "type": "function", "function": {
            "name": "analyze_arrival", "arguments": json.dumps({"order_id": "PO-1001"}),
        }},
        {"id": "call-sop", "type": "function", "function": {
            "name": "search_sop", "arguments": json.dumps({"query": "逾期未收货 物流异常 供应商跟进"}),
        }},
    ]

    def handle(request):
        assert request.method == "POST"
        assert str(request.url) == base_url + "/chat/completions"
        assert request.headers["authorization"] == "Bearer " + dummy_token
        body = json.loads(request.content)
        requests.append(body)
        assert len(requests) <= 2, "Valid structured final JSON should not need a correction round"
        assert body["model"] == "qwen-plus"
        assert body["temperature"] == 0
        assert body["enable_thinking"] is False
        assert body.get("stream", False) is False
        assert dummy_token not in request.content.decode()

        definitions = {item["function"]["name"]: item for item in body["tools"]}
        assert set(definitions) == {"get_order_snapshot", "analyze_arrival", "search_sop"}
        for name, required in [("get_order_snapshot", "order_id"),
                               ("analyze_arrival", "order_id"), ("search_sop", "query")]:
            assert definitions[name]["type"] == "function"
            function = definitions[name]["function"]
            assert function["description"]
            assert function["parameters"]["properties"][required]["type"] == "string"
            assert required in function["parameters"]["required"]

        messages = body["messages"]
        assert messages[0]["role"] == "system"
        assert "PO-1001" in messages[1]["content"]
        if len(requests) == 1:
            assert not any(message["role"] == "tool" for message in messages)
            response_message = {"role": "assistant", "content": None, "tool_calls": provider_tool_calls}
            finish_reason = "tool_calls"
            usage = {"prompt_tokens": 31, "completion_tokens": 9, "total_tokens": 40}
        else:
            assistant_calls = [message for message in messages if message["role"] == "assistant"]
            assert len(assistant_calls) == 1
            replayed_calls = assistant_calls[0]["tool_calls"]
            assert len(replayed_calls) == len(provider_tool_calls)
            for replayed, original in zip(replayed_calls, provider_tool_calls, strict=True):
                assert replayed["id"] == original["id"]
                assert replayed["type"] == original["type"]
                assert replayed["function"]["name"] == original["function"]["name"]
                # The SDK may normalize Unicode escaping while reserializing JSON.
                assert json.loads(replayed["function"]["arguments"]) == json.loads(original["function"]["arguments"])
            tool_results = {
                message["tool_call_id"]: json.loads(message["content"])
                for message in messages if message["role"] == "tool"
            }
            assert set(tool_results) == {"call-analysis", "call-sop"}
            assert tool_results["call-analysis"]["order_id"] == "PO-1001"
            assert tool_results["call-analysis"]["facts"]["outstanding_qty"] == 400
            assert tool_results["call-sop"]
            assert all(ref["id"].startswith("sop:") for ref in tool_results["call-sop"])
            response_message = {"role": "assistant", "content": '{"actions":[],"notes":[]}'}
            finish_reason = "stop"
            usage = {"prompt_tokens": 47, "completion_tokens": 11, "total_tokens": 58}
        return httpx.Response(200, json={
            "id": f"chatcmpl-offline-{len(requests)}", "object": "chat.completion",
            "created": 1792800000, "model": "qwen-plus",
            "choices": [{"index": 0, "message": response_message, "finish_reason": finish_reason}],
            "usage": usage,
        })

    def checked_handle(request):
        try:
            return handle(request)
        except AssertionError as exc:
            transport_errors.append(str(exc))
            raise

    database_url = f"sqlite:///{(tmp_path / 'transport.db').as_posix()}"
    engine = make_engine(database_url)
    init_database(engine, seed=True)
    settings = Settings(
        _env_file=None, database_url=database_url, agent_mode="cloud", embedding_mode="local",
        model_api_key=dummy_token, model_base_url=base_url, model_name="qwen-plus",
        index_dir=tmp_path / "index", as_of_date=date(2026, 10, 24),
        request_timeout_seconds=15, model_timeout_seconds=5,
    )
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(checked_handle)) as mock_client:
            model = ChatOpenAI(
                model="qwen-plus", api_key=dummy_token, base_url=base_url,
                http_async_client=mock_client, temperature=0, timeout=5,
                max_retries=0, extra_body={"enable_thinking": False},
            )
            service = AgentService(engine, settings, model_factory=lambda: model)
            result = await service.run("分析 PO-1001 为什么没到齐")

        assert not transport_errors, transport_errors
        assert result["status"] == "completed", result["report"] or result["answer"]
        assert len(requests) == 2
        assert result["mode"] == "cloud"
        assert result["metrics"]["model_rounds"] == 2
        assert result["metrics"]["tool_calls"] == 3
        assert result["metrics"]["input_tokens"] == 78
        assert result["metrics"]["output_tokens"] == 20
        assert result["report"]["facts"]["ordered_qty"] == 1000
        assert result["report"]["facts"]["received_qty"] == 600
        assert result["report"]["facts"]["outstanding_qty"] == 400
        assert result["report"]["actions"] == []
        assert {item["code"] for item in result["report"]["anomalies"]} == {
            "OVERDUE_UNRECEIVED", "LOGISTICS_EXCEPTION",
        }
        assert all(step["status"] == "ok" for step in result["steps"])
        assert result["report"]["sop_refs"]
        persisted = load_run(engine, result["run_id"])
        assert persisted == result
        assert dummy_token not in json.dumps(result, ensure_ascii=False)
        assert dummy_token not in json.dumps(persisted, ensure_ascii=False)
    finally:
        engine.dispose()
