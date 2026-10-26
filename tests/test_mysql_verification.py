"""Offline checks for verification logic; these do not claim a live MySQL run."""
import importlib.util
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError

from supplylens.config import ROOT
from supplylens.db import make_engine


def verifier():
    spec = importlib.util.spec_from_file_location("verify_mysql", ROOT / "scripts/verify_mysql.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_application_verification_exercises_real_rules_imports_and_storage():
    module = verifier()
    engine = make_engine("sqlite:///:memory:")
    checks = []
    try:
        module.verify_application(engine, checks)
    finally:
        engine.dispose()
    assert len(checks) == 8
    assert all(check["status"] == "passed" for check in checks)
    assert checks[-1]["detail"]["model_calls"] == 0
    assert checks[6]["detail"]["earlier_supplier_insert_rolled_back"] is True


@pytest.mark.parametrize("url", ["", "sqlite:///:memory:", "mysql://root:secret@localhost/db"])
def test_cli_rejects_missing_or_non_pymysql_urls_without_exposing_password(url):
    report = verifier().run_verification(url)
    assert report["status"] == "failed"
    assert report["checks"] == []
    assert "secret" not in json.dumps(report)


def test_connection_exception_does_not_leak_password_url_or_local_paths(monkeypatch):
    module = verifier()

    def fail(*args, **kwargs):
        raise OperationalError("private-driver-path", {}, RuntimeError("mysql+pymysql://root:secret@private-host/private-db"))

    monkeypatch.setattr(module, "create_engine", fail)
    report = module.run_verification("mysql+pymysql://root:secret@private-host/private-db")
    serialized = json.dumps(report)
    assert report["status"] == "failed"
    assert "secret" not in serialized and "private-host" not in serialized and "private-driver-path" not in serialized


def test_verification_failure_is_recorded_and_not_replaced_by_success():
    module = verifier()
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE unrelated (id INTEGER)")
    checks = []
    try:
        with pytest.raises(module.VerificationFailure):
            module.verify_application(engine, checks)
    finally:
        engine.dispose()
    assert checks[0]["status"] == "failed"
    assert len(checks) == 1


def test_disabled_database_foreign_keys_cannot_be_reported_as_passing():
    module = verifier()
    # A plain SQLite engine intentionally lacks the project's FK pragma.
    engine = create_engine("sqlite:///:memory:")
    checks = []
    try:
        with pytest.raises(module.VerificationFailure):
            module.verify_application(engine, checks)
    finally:
        engine.dispose()
    assert checks[-1]["name"] == "database_foreign_key_and_transaction_rollback"
    assert checks[-1]["status"] == "failed"


def test_server_url_database_is_not_used_and_only_new_schema_is_created(monkeypatch):
    module = verifier()
    recorded = {}

    class Server:
        def connect(self):
            return self

        def execution_options(self, **kwargs):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, statement):
            recorded.setdefault("sql", []).append(str(statement))
            return self

        def scalar_one(self):
            return "8.4.7"

        def dispose(self):
            pass

    def server_factory(url, **kwargs):
        recorded["server_url"] = url
        return Server()

    def schema_factory(url):
        from sqlalchemy.engine import make_url
        recorded["schema_url"] = make_url(url)
        return Server()

    monkeypatch.setattr(module, "create_engine", server_factory)
    monkeypatch.setattr(module, "make_engine", schema_factory)
    monkeypatch.setattr(module, "verify_application", lambda engine, checks: None)
    report = module.run_verification("mysql+pymysql://root:secret@private-host/existing_business")
    assert not recorded["server_url"].database
    assert recorded["schema_url"].database == report["schema"]
    assert module.SCHEMA_PATTERN.fullmatch(report["schema"])
    assert len(recorded["sql"]) == 2
    assert recorded["sql"][1].startswith("CREATE DATABASE `supplylens_verification_")
    assert all("DROP" not in sql and "existing_business" not in sql for sql in recorded["sql"])
    assert "secret" not in json.dumps(report) and "private-host" not in json.dumps(report)


def test_public_artifact_only_serializes_actual_result(monkeypatch, tmp_path, capsys):
    module = verifier()
    monkeypatch.delenv("MYSQL_VERIFICATION_URL", raising=False)
    output = tmp_path / "mysql.json"
    assert module.main(["--output", str(output)]) == 1
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["status"] == "failed" and result["checks"] == []
    assert str(output) not in capsys.readouterr().out
