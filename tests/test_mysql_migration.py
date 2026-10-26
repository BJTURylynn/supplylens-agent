import importlib.util
from datetime import date, datetime

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from supplylens.config import ROOT
from supplylens.db import Base, OrderOrigin, PurchaseOrder, Run, Supplier, init_database, make_engine

spec = importlib.util.spec_from_file_location("mysql_migration", ROOT / "scripts/migrate_sqlite_to_mysql.py")
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


@pytest.fixture
def source_file(tmp_path):
    path = tmp_path / "original.db"
    engine = make_engine("sqlite:///" + path.as_posix())
    init_database(engine)
    with Session(engine) as session, session.begin():
        session.add(OrderOrigin(order_id="PO-1001", batch_id="sample", source_name="模拟导入.xlsx",
                                imported_at=datetime(2026, 10, 25, 9, 1, 2, 123456), as_of=date(2026, 10, 24),
                                coverage={"receipts": True, "unknown": None}))
        session.add(Run(id="saved-run", created_at=datetime(2026, 10, 25, 9, 1, 3, 654321),
                        payload={"status": "completed", "facts": {"qty": 400}, "notes": ["模拟资料", None]}))
    engine.dispose()
    return path


def test_copy_all_tables_keeps_json_dates_and_source_bytes(source_file):
    before = source_file.read_bytes()
    source = migration.open_readonly_source(source_file)
    target = make_engine("sqlite:///:memory:")
    try:
        counts = migration.copy_into_empty_database(source, target)
        assert set(counts) == set(Base.metadata.tables)
        assert counts["purchase_orders"] == 100 and counts["suppliers"] == 10
        assert counts["order_origins"] == counts["runs"] == 1
        with Session(target) as session:
            assert session.get(Run, "saved-run").payload["notes"] == ["模拟资料", None]
            assert session.get(Run, "saved-run").created_at.microsecond == 654321
            assert session.get(OrderOrigin, "PO-1001").coverage == {"receipts": True, "unknown": None}
            assert session.get(OrderOrigin, "PO-1001").imported_at.microsecond == 123456
            assert session.get(PurchaseOrder, "PO-1001").promised_date == date(2026, 10, 22)
        assert source_file.read_bytes() == before
    finally:
        source.dispose()
        target.dispose()


def test_nonempty_destination_is_refused_without_overwriting(source_file):
    source = migration.open_readonly_source(source_file)
    target = make_engine("sqlite:///:memory:")
    init_database(target, seed=False)
    with Session(target) as session, session.begin():
        session.add(Supplier(id="existing", name="已有资料"))
    try:
        with pytest.raises(migration.MigrationRefused, match="Destination contains data"):
            migration.copy_into_empty_database(source, target)
        with Session(target) as session:
            assert session.get(Supplier, "existing").name == "已有资料"
            assert session.scalar(select(func.count()).select_from(Supplier)) == 1
            assert session.scalar(select(func.count()).select_from(Run)) == 0
    finally:
        source.dispose()
        target.dispose()


def test_insert_failure_rolls_back_every_table(source_file):
    source = migration.open_readonly_source(source_file)
    target = make_engine("sqlite:///:memory:")

    def fail_insert(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO shipments"):
            raise RuntimeError("Injected later-table failure")

    event.listen(target, "before_cursor_execute", fail_insert)
    try:
        with pytest.raises(RuntimeError, match="Injected later-table"):
            migration.copy_into_empty_database(source, target)
        with target.connect() as connection:
            assert all(connection.scalar(select(func.count()).select_from(table)) == 0
                       for table in Base.metadata.sorted_tables)
    finally:
        source.dispose()
        target.dispose()


def test_source_connection_is_readonly_and_missing_file_not_created(source_file, tmp_path):
    source = migration.open_readonly_source(source_file)
    try:
        with source.begin() as connection, pytest.raises(OperationalError, match="readonly"):
            connection.execute(Supplier.__table__.insert().values(id="not-allowed", name="禁止写入"))
    finally:
        source.dispose()
    missing = tmp_path / "missing.db"
    with pytest.raises(migration.MigrationRefused, match="does not exist"):
        migration.open_readonly_source(missing)
    assert not missing.exists()


@pytest.mark.parametrize("url", ["", "sqlite:///local.db", "mysql://u:secret@host/db", "mysql+pymysql://u:secret@host"])
def test_cli_target_requires_explicit_mysql_database_without_echoing_credentials(url):
    with pytest.raises(migration.MigrationRefused) as exc:
        migration.validate_target_url(url)
    assert "secret" not in str(exc.value)


def test_report_write_failure_does_not_claim_committed_data_was_rolled_back(source_file, tmp_path, monkeypatch, capsys):
    import json
    import sys

    target = make_engine("sqlite:///:memory:")
    monkeypatch.setenv("MYSQL_TARGET_URL", "mysql+pymysql://u:secret@host/empty_schema")
    monkeypatch.setattr(migration, "make_engine", lambda _: target)
    monkeypatch.setattr(sys, "argv", ["migrate_sqlite_to_mysql.py", "--source", str(source_file),
                                     "--output", str(tmp_path)])
    assert migration.main() == 2
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "migrated" and result["report_saved"] is False
    assert "rolled back" not in result["message"]
    assert "secret" not in str(result)
