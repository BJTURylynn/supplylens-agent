"""Explicit live-provider check using harmless canary text, never business records."""
import asyncio
import time
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx
import numpy as np
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

from supplylens.config import Settings
from supplylens.provider import ProviderQuotaError, validate_provider_reply


def cloud_status(settings: Settings) -> dict:
    return {"configured": bool(settings.model_api_key.strip()), "model": settings.model_name,
            "embedding_model": settings.embedding_model, "endpoint_host": urlsplit(settings.model_base_url).hostname,
            "agent_mode": settings.agent_mode, "embedding_mode": settings.embedding_mode}


def _error(exc: Exception) -> dict:
    if isinstance(exc, ProviderQuotaError):
        return {"code": "trial_quota", "message": str(exc)}
    status = getattr(exc, "status_code", None)
    if status is None and isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
    if status in (401, 403):
        return {"code": "authentication", "message": "密钥无效、服务区域不匹配或模型权限不足。", "http_status": status}
    if status == 429:
        return {"code": "rate_limit", "message": "服务限流或额度不足，请在模型服务控制台核对。", "http_status": status}
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)) or "Timeout" in type(exc).__name__:
        return {"code": "timeout", "message": "模型服务响应超时。"}
    if isinstance(exc, (httpx.RequestError,)) or "Connection" in type(exc).__name__:
        return {"code": "connection", "message": "无法连接模型服务，请检查网络与 API 地址。"}
    return {"code": "provider_or_protocol", "message": "服务或响应协议不兼容，请核对模型名和工具调用/向量接口。",
            "error_type": type(exc).__name__}


async def check_cloud(settings: Settings, *, transport=None) -> dict:
    started = time.monotonic()
    result = {"status": "not_configured", "checked_at": datetime.now(UTC).isoformat(),
              **cloud_status(settings), "stages": [], "sent_business_data": False,
              "input_tokens": None, "output_tokens": None, "embedding_tokens": None}
    if not result["configured"]:
        result["message"] = "请在服务端本地 .env 设置 MODEL_API_KEY；不要发送到聊天或提交到 GitHub。"
        result["elapsed_ms"] = 0
        return result

    @tool
    def supplylens_connection_probe() -> dict:
        """返回 SupplyLens 联调探针的固定结果；不访问任何订单或数据库。"""
        return {"probe": "supplylens", "status": "ok"}

    async def perform():
        async with httpx.AsyncClient(timeout=settings.model_timeout_seconds, transport=transport) as client:
            if settings.embedding_mode == "cloud":
                try:
                    response = await client.post(
                        settings.model_base_url.rstrip("/") + "/embeddings",
                        headers={"Authorization": "Bearer " + settings.model_api_key},
                        json={"model": settings.embedding_model, "input": ["SupplyLens 采购到货连接测试"],
                              "dimensions": settings.embedding_dimensions, "encoding_format": "float"},
                    )
                    response.raise_for_status()
                    payload = response.json()
                    data = payload["data"]
                    if len(data) != 1 or type(data[0]["index"]) is not int or data[0]["index"] != 0:
                        raise ValueError("embedding response index/count mismatch")
                    vector = np.asarray(data[0]["embedding"], dtype=np.float64)
                    if vector.shape != (settings.embedding_dimensions,) or not np.isfinite(vector).all() or np.linalg.norm(vector) == 0:
                        raise ValueError("invalid embedding vector")
                    tokens = payload.get("usage", {}).get("total_tokens")
                    result["embedding_tokens"] = tokens if type(tokens) is int else None
                    result["stages"].append({"stage": "embedding", "status": "ok", "dimensions": len(vector)})
                except Exception as exc:
                    result["stages"].append({"stage": "embedding", "status": "failed", **_error(exc)})
            else:
                result["stages"].append({"stage": "embedding", "status": "skipped", "reason": "local_retrieval"})
            try:
                model = ChatOpenAI(model=settings.model_name, api_key=settings.model_api_key,
                                   base_url=settings.model_base_url, temperature=0, max_retries=0,
                                   timeout=settings.model_timeout_seconds, http_async_client=client,
                                   extra_body=settings.model_extra_body)
                messages = [SystemMessage(content="你正在执行连接检查，请按要求调用探针工具；不查询业务数据。"),
                            HumanMessage(content="请调用 supplylens_connection_probe 检查连接。")]
                first = await model.bind_tools([supplylens_connection_probe], tool_choice="supplylens_connection_probe").ainvoke(messages)
                validate_provider_reply(first)
                if len(first.tool_calls) != 1 or first.tool_calls[0]["name"] != "supplylens_connection_probe" or first.tool_calls[0]["args"]:
                    raise ValueError("model did not issue the expected tool call")
                call = first.tool_calls[0]
                second = await model.ainvoke(messages + [first, ToolMessage(
                    content='{"probe":"supplylens","status":"ok"}', tool_call_id=call["id"]),
                    HumanMessage(content="探针已返回，请简短确认连接检查完成。")])
                validate_provider_reply(second)
                if not second.content or second.tool_calls:
                    raise ValueError("model did not finish after the tool result")
                for key in ("input_tokens", "output_tokens"):
                    usages = [(message.usage_metadata or {}).get(key) for message in (first, second)]
                    result[key] = sum(usages) if all(type(value) is int for value in usages) else None
                result["stages"].append({"stage": "chat_tool_roundtrip", "status": "ok", "model_rounds": 2})
            except Exception as exc:
                result["stages"].append({"stage": "chat_tool_roundtrip", "status": "failed", **_error(exc)})

    try:
        await asyncio.wait_for(perform(), timeout=settings.request_timeout_seconds)
    except TimeoutError:
        result["stages"].append({"stage": "overall", "status": "failed", **_error(TimeoutError())})
    result["status"] = "ok" if len(result["stages"]) == 2 and all(s["status"] in ("ok", "skipped") for s in result["stages"]) else "failed"
    result["elapsed_ms"] = round((time.monotonic() - started) * 1000, 2)
    result["message"] = ("真实聊天工具调用已连通；文档检索使用本地模式，未调用向量接口。" if settings.embedding_mode == "local" else "真实聊天工具调用与向量接口均已连通。") if result["status"] == "ok" else "连接检查未全部通过，请查看各阶段结果。"
    return result
