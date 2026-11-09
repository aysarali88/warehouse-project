"""Forward-only metadata migration for post-cutover Rollout eligibility."""

import re
from datetime import datetime, timezone

from sqlalchemy import inspect, text

from migrations.site_inventory_migration_utils import (
    ensure_version_table,
    qualified,
    record_version,
    require_table_columns,
    resolve_postgres_schema,
    version_applied,
)

VERSION = "site-inventory-rollout-cutover-v1"
COLUMN = "site_inventory_recorded_at"


def _validate_schema(schema: str | None, dialect: str) -> str | None:
    if dialect == "sqlite":
        if schema in (None, "", "main"):
            return None
        raise ValueError("SQLite rollout cutover migration uses the default isolated database schema")
    value = str(schema or "").strip()
    if value and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", value):
        raise ValueError("Invalid PostgreSQL schema identifier")
    return value or None


def _validate_existing_column(connection, schema: str | None) -> bool:
    columns = {column["name"]: column for column in inspect(connection).get_columns("rollout_records", schema=schema)}
    column = columns.get(COLUMN)
    if column is None:
        return False
    type_name = str(column["type"]).upper()
    is_timestamp = "TIMESTAMP" in type_name or "DATETIME" in type_name
    timezone_enabled = getattr(column["type"], "timezone", None)
    if (
        not is_timestamp
        or column["nullable"] is not True
        or (connection.dialect.name == "postgresql" and timezone_enabled is not True)
    ):
        raise RuntimeError("Unexpected rollout cutover timestamp column; migration stopped")
    return True


def migrate(bind, schema: str | None = None) -> dict:
    """Add a nullable server-stamp column; existing Rollout rows remain ineligible."""
    dialect = bind.dialect.name
    if dialect not in {"postgresql", "sqlite"}:
        raise RuntimeError(f"Unsupported rollout cutover migration dialect: {dialect}")

    with bind.connect() as connection:
        transaction = connection.begin()
        try:
            if dialect == "postgresql":
                schema_name = resolve_postgres_schema(connection, _validate_schema(schema, dialect))
                require_table_columns(connection, schema_name, {
                    "rollout_records": {"id", "record_id", "date", "entry_time", "status", "area"},
                })
                ensure_version_table(connection, schema_name)
                if version_applied(connection, schema_name, VERSION):
                    if not _validate_existing_column(connection, schema_name):
                        raise RuntimeError("Recorded rollout cutover migration is missing its column")
                    transaction.commit()
                    return {"version": VERSION, "schema": schema_name, "already_applied": True, "column": COLUMN}

                existing_column = _validate_existing_column(connection, schema_name)
                if existing_column:
                    target = qualified(connection, schema_name, "rollout_records")
                    non_null_count = connection.execute(text(
                        f"SELECT count(*) FROM {target} WHERE {connection.dialect.identifier_preparer.quote(COLUMN)} IS NOT NULL"
                    )).scalar_one()
                    if non_null_count:
                        raise RuntimeError("Unversioned rollout cutover timestamps already contain data; migration stopped")
                else:
                    target = qualified(connection, schema_name, "rollout_records")
                    connection.exec_driver_sql(
                        f"ALTER TABLE {target} ADD COLUMN {connection.dialect.identifier_preparer.quote(COLUMN)} TIMESTAMPTZ NULL"
                    )
                record_version(connection, schema_name, VERSION)
            else:
                if not inspect(connection).has_table("rollout_records"):
                    raise RuntimeError("Required pre-existing table rollout_records is missing")
                if not inspect(connection).has_table("site_inventory_schema_versions"):
                    raise RuntimeError("Site Inventory Foundation is required before rollout cutoff protection")
                if connection.execute(
                    text("SELECT 1 FROM site_inventory_schema_versions WHERE version=:version"),
                    {"version": VERSION},
                ).first() is not None:
                    if not _validate_existing_column(connection, None):
                        raise RuntimeError("Recorded rollout cutover migration is missing its column")
                    transaction.commit()
                    return {"version": VERSION, "schema": "main", "already_applied": True, "column": COLUMN}
                if not _validate_existing_column(connection, None):
                    connection.exec_driver_sql(f"ALTER TABLE rollout_records ADD COLUMN {COLUMN} DATETIME NULL")
                connection.execute(
                    text("INSERT INTO site_inventory_schema_versions (version, applied_at) VALUES (:version, :at)"),
                    {"version": VERSION, "at": datetime.now(timezone.utc).isoformat()},
                )
                schema_name = "main"
            transaction.commit()
        except Exception:
            transaction.rollback()
            raise
    return {"version": VERSION, "schema": schema_name, "already_applied": False, "column": COLUMN}
