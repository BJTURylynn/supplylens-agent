from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from supplylens.api import create_app
from supplylens.config import ROOT, Settings


def config(tmp_path: Path, **overrides) -> Settings:
    return Settings(_env_file=None, database_url="sqlite:///:memory:", agent_mode="demo",
                    embedding_mode="local", sop_dir=ROOT / "data/sop", index_dir=tmp_path / "index", **overrides)


def test_health_orders_chat_and_persisted_run(tmp_path):
    with TestClient(create_app(config(tmp_path))) as client:
        health = client.get("/health").json()
        assert health["status"] == health["database"] == "ok"
        assert health["database_backend"] == "sqlite"
        assert health["agent_mode"] == "demo" and health["as_of"] == "2026-10-24"
        orders = client.get("/api/orders").json()["items"]
        assert len(orders) == 100 and orders[0]["order_id"] == "PO-1001"
        response = client.post("/api/chat", json={"message": "分析采购单PO-1001为什么未到齐？"})
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "completed"
        assert body["report"]["facts"]["outstanding_qty"] == 400
        assert body["metrics"]["input_tokens"] is None
        assert body["metrics"]["model_rounds"] == 0
        saved = client.get("/api/runs/" + body["run_id"]).json()
        assert saved["report"] == body["report"]
        assert [s["name"] for s in saved["steps"]] == ["get_order_snapshot", "analyze_arrival", "search_sop"]
        followup = client.post("/api/chat", json={"message": "帮我写供应商跟进建议", "session_id": body["session_id"]}).json()
        assert followup["report"]["order_id"] == "PO-1001"
        assert "400" in followup["report"]["followup_draft"]
        assert "尚未发送" in followup["markdown"]


@pytest.mark.parametrize("payload", [
    {"message": ""}, {"message": "  \n"}, {"message": "x" * 2001}, {"message": "分析PO-1001", "sql": "DELETE"},
    {"message": "分析PO-1001", "session_id": "s" * 65}, {},
])
def test_request_validation(tmp_path, payload):
    with TestClient(create_app(config(tmp_path))) as client:
        assert client.post("/api/chat", json=payload).status_code == 422


def test_unavailable_database_reports_degraded_without_credentials(tmp_path):
    settings = config(tmp_path)
    settings.database_url = "invalid+driver://user:secret-password@host/db"
    with TestClient(create_app(settings)) as client:
        health = client.get("/health")
        assert health.json()["status"] == "degraded"
        assert "secret-password" not in health.text
        response = client.post("/api/chat", json={"message": "分析PO-1001"})
        assert response.status_code == 503
        assert "secret-password" not in response.text


def test_restarted_api_preserves_reports_but_not_session_context(tmp_path):
    settings = config(tmp_path)
    settings.database_url = "sqlite:///" + (tmp_path / "persist.db").as_posix()
    with TestClient(create_app(settings)) as first:
        old_boot = first.get("/health").json()["boot_id"]
        saved = first.post("/api/chat", json={"message": "分析PO-1001"}).json()
    with TestClient(create_app(settings)) as second:
        assert second.get("/health").json()["boot_id"] != old_boot
        assert second.get("/api/runs/" + saved["run_id"]).status_code == 200
        response = second.post("/api/chat", json={"message": "帮我跟进", "session_id": saved["session_id"]}).json()
        assert response["status"] == "needs_input" and response["session_reset"] is True
        assert response["session_id"] != saved["session_id"]
        assert response["report"] is None


def test_query_bounds_and_unknown_run(tmp_path):
    with TestClient(create_app(config(tmp_path))) as client:
        assert client.get("/api/orders?limit=0").status_code == 422
        assert client.get("/api/orders?limit=1001").status_code == 422
        assert len(client.get("/api/orders?limit=3").json()["items"]) == 3
        assert client.get("/api/runs/missing").status_code == 404
        assert client.get("/api/runs/" + "x" * 65).status_code == 404


def test_cloud_missing_credentials_is_explicit_not_demo_fallback(tmp_path):
    settings = config(tmp_path)
    settings.agent_mode = "cloud"
    settings.model_api_key = ""
    with TestClient(create_app(settings)) as client:
        result = client.post("/api/chat", json={"message": "分析PO-1001"}).json()
        assert result["status"] == "failed"
        assert result["mode"] == "cloud"
        assert result["report"] is None
        assert "MODEL_API_KEY" in result["answer"]


@pytest.mark.parametrize("ambiguous", ["分析PO-abc", "分析PO-1001和PO-1004"])
def test_ambiguous_explicit_order_clears_previous_context(tmp_path, ambiguous):
    with TestClient(create_app(config(tmp_path))) as client:
        first = client.post("/api/chat", json={"message": "分析PO-1001"}).json()
        sid = first["session_id"]
        ambiguity = client.post("/api/chat", json={"message": ambiguous, "session_id": sid}).json()
        assert ambiguity["status"] == "needs_input"
        following = client.post("/api/chat", json={"message": "帮我跟进", "session_id": sid}).json()
        assert following["status"] == "needs_input" and following["report"] is None

