"""Copy a read-only SQLite snapshot into an empty MySQL schema, without deleting data.

The destination URL is read only from MYSQL_TARGET_URL, never from a CLI argument.
DDL can remain after a failed copy; data inserts run in one transaction. Use a new,
exclusive destination schema and retain the original SQLite file as a backup.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.pool import NullPool

from supplylens.db import Base, make_engine


class MigrationRefused(ValueError):
    """A controlled precondition failure safe to show without connection details."""


def open_readonly_source(path: Path) -> Engine:
    path = path.resolve()
    if not path.is_file():
        raise MigrationRefused("SQLite source file does not exist; no new source file was created.")
    return create_engine(
        "sqlite+pysqlite://",
        creator=lambda: sqlite3.connect(path.as_uri() + "?mode=ro", uri=True),
        poolclass=NullPool,
    )


def validate_target_url(value: str):
    try:
        url = make_url(value)
    except Exception:
        raise MigrationRefused("MYSQL_TARGET_URL must be a mysql+pymysql URL with a database name.") from None
    if url.drivername != "mysql+pymysql" or not url.database:
        raise MigrationRefused("MYSQL_TARGET_URL must be a mysql+pymysql URL with a database name.")
    return url


def _json_value(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError("Unexpected database value type")


def _canonical_rows(rows: list[dict]) -> list[str]:
    return sorted(json.dumps(row, ensure_ascii=False, sort_keys=True, default=_json_value) for row in rows)


def copy_into_empty_database(source: Engine, target: Engine) -> dict[str, int]:
    """Copy all eight known tables and compare every value before commit.

    SQLite targets are permitted here to exercise transactions in offline tests;
    the command-line entry point explicitly requires a MySQL destination.
    """
    if source.dialect.name != "sqlite":
        raise MigrationRefused("The migration source must be SQLite.")
    tables = Base.metadata.sorted_tables
    with source.connect() as original:
        # Python sqlite3's legacy transaction mode does not begin on SELECT.
        # Explicit BEGIN gives the table reads one consistent source snapshot.
        original.exec_driver_sql("BEGIN")
        source_rows = {table.name: [dict(row) for row in original.execute(select(table)).mappings()]
                       for table in tables}
        Base.metadata.create_all(target)
        with target.begin() as destination:
            for table in tables:
                if destination.scalar(select(func.count()).select_from(table)):
                    raise MigrationRefused("Destination contains data; use a new empty schema. Nothing was overwritten.")
            for table in tables:
                rows = source_rows[table.name]
                if rows:
                    destination.execute(table.insert(), rows)
            counts = {}
            for table in tables:
                actual = [dict(row) for row in destination.execute(select(table)).mappings()]
                expected = source_rows[table.name]
                if _canonical_rows(actual) != _canonical_rows(expected):
                    raise MigrationRefused("Destination values differ from the source; inserted rows were rolled back.")
                counts[table.name] = len(actual)
        return counts


def main() -> int:
    parser = argparse.ArgumentParser(description="Copy existing SQLite data to an empty MySQL schema.")
    parser.add_argument("--source", type=Path, default=Path("runtime/supplylens.db"))
    parser.add_argument("--output", type=Path, default=Path("runtime/mysql-migration.json"))
    args = parser.parse_args()
    source = target = None
    committed = False
    try:
        value = os.getenv("MYSQL_TARGET_URL", "")
        if not value:
            raise MigrationRefused("Set MYSQL_TARGET_URL locally; do not put database credentials on the command line.")
        target_url = validate_target_url(value)
        source = open_readonly_source(args.source)
        target = make_engine(target_url.render_as_string(hide_password=False))
        counts = copy_into_empty_database(source, target)
        committed = True
        result = {"status": "migrated", "checked_at": datetime.now(UTC).isoformat(),
                  "source_dialect": "sqlite", "target_dialect": target.dialect.name,
                  "table_counts": counts, "content_verified": True, "source_preserved": True}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except MigrationRefused as exc:
        print(json.dumps({"status": "refused", "message": str(exc)}, ensure_ascii=False))
        return 1
    except Exception as exc:
        if committed:
            print(json.dumps({"status": "migrated", "report_saved": False,
                              "error_type": type(exc).__name__,
                              "message": "Migration committed, but the report file could not be saved. Do not rerun into this populated schema."}))
            return 2
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__,
                          "message": "Migration failed; SQLite source retained. Destination data inserts were rolled back."}))
        return 1
    finally:
        if source is not None:
            source.dispose()
        if target is not None:
            target.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
