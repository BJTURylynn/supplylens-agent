import json

import httpx
import pytest
from fastapi.testclient import TestClient

from supplylens.api import create_app
from supplylens.cloud import check_cloud
from supplylens.config import Settings

KEY = "private-cloud-check-key-never-return"


def config(**kwargs):
    return Settings(_env_file=None, model_api_key=KEY, model_base_url="https://probe.example/v1",
                    embedding_dimensions=4, model_timeout_seconds=.5, **kwargs)


def chat_response(tool_call: bool):
    return {"id": "canary-check", "object": "chat.completion", "created": 1, "model": "qwen-plus",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": None,
                        "tool_calls": [{"id": "probe-call", "type": "function", "function": {
                            "name": "supplylens_connection_probe", "arguments": "{}"}}]} if tool_call else
                        {"role": "assistant", "content": "连接检查完成。"},
                        "finish_reason": "tool_calls" if tool_call else "stop"}],
            "usage": {"prompt_tokens": 10 if tool_call else 12, "completion_tokens": 4 if tool_call else 5, "total_tokens": 14 if tool_call else 17}}


async def test_live_check_uses_real_sdk_over_mock_http_and_never_reads_orders():
    requests = []

    def handler(request):
        assert request.headers["authorization"] == "Bearer " + KEY
        payload = json.loads(request.content)
        requests.append(payload)
        assert KEY not in request.content.decode()
        assert "PO-1001" not in request.content.decode()
        if request.url.path.endswith("embeddings"):
            assert payload["input"] == ["SupplyLens 采购到货连接测试"]
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1, .2, .3, .4]}], "usage": {"total_tokens": 8}})
        if len(requests) == 2:
            assert payload["tool_choice"]["function"]["name"] == "supplylens_connection_probe"
            assert payload["tools"][0]["function"]["name"] == "supplylens_connection_probe"
            assert payload["enable_thinking"] is False
            return httpx.Response(200, json=chat_response(True))
        tool_messages = [message for message in payload["messages"] if message["role"] == "tool"]
        assert tool_messages[0]["tool_call_id"] == "probe-call"
        assert json.loads(tool_messages[0]["content"])["status"] == "ok"
        return httpx.Response(200, json=chat_response(False))

    result = await check_cloud(config(), transport=httpx.MockTransport(handler))
    assert result["status"] == "ok", result
    assert result["sent_business_data"] is False
    assert result["input_tokens"] == 22 and result["output_tokens"] == 9
    assert result["embedding_tokens"] == 8
    assert len(requests) == 3
    assert KEY not in json.dumps(result, ensure_ascii=False)


async def test_missing_key_never_calls_provider():
    def fail(request):
        raise AssertionError("Must not contact the provider")

    settings = config()
    settings.model_api_key = ""
    result = await check_cloud(settings, transport=httpx.MockTransport(fail))
    assert result["status"] == "not_configured"
    assert result["stages"] == []


@pytest.mark.parametrize("status,code", [(401, "authentication"), (403, "authentication"), (429, "rate_limit"), (500, "provider_or_protocol")])
async def test_provider_http_failures_are_sanitized(status, code):
    def handler(request):
        return httpx.Response(status, json={"error": {"message": KEY, "type": "api_error"}})

    result = await check_cloud(config(), transport=httpx.MockTransport(handler))
    assert result["status"] == "failed"
    assert all(stage["code"] == code for stage in result["stages"])
    assert KEY not in json.dumps(result)


async def test_timeout_is_explicit_and_sanitized():
    def handler(request):
        raise httpx.ReadTimeout(KEY)

    result = await check_cloud(config(), transport=httpx.MockTransport(handler))
    assert result["status"] == "failed"
    assert all(stage["code"] == "timeout" for stage in result["stages"])
    assert KEY not in json.dumps(result)


@pytest.mark.parametrize("vector", [[0, 0, 0, 0], [1], [1, 2, 3, "NaN"]])
async def test_invalid_embedding_does_not_report_full_connectivity(vector):
    calls = []

    def handler(request):
        if request.url.path.endswith("embeddings"):
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": vector}]})
        calls.append(request)
        return httpx.Response(200, json=chat_response(len(calls) == 1))

    result = await check_cloud(config(), transport=httpx.MockTransport(handler))
    assert result["status"] == "failed"
    assert result["stages"][0]["status"] == "failed"
    assert result["stages"][1]["status"] == "ok"


async def test_plain_text_instead_of_tool_call_fails_probe():
    def handler(request):
        if request.url.path.endswith("embeddings"):
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1, 2, 3, 4]}]})
        return httpx.Response(200, json=chat_response(False))

    result = await check_cloud(config(), transport=httpx.MockTransport(handler))
    assert result["status"] == "failed"
    assert result["stages"][1]["code"] == "provider_or_protocol"


def test_cloud_status_route_never_returns_key_and_missing_check_is_honest(tmp_path):
    settings = Settings(_env_file=None, database_url="sqlite:///:memory:", model_api_key="", index_dir=tmp_path)
    with TestClient(create_app(settings)) as client:
        status = client.get("/api/cloud/status").json()
        assert status["configured"] is False
        assert status["endpoint_host"] == "dashscope.aliyuncs.com"
        checked = client.post("/api/cloud/check").json()
        assert checked["status"] == "not_configured"
        assert checked["sent_business_data"] is False
