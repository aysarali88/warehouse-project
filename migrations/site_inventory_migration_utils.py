"""Schema discovery and fail-closed preflight helpers for Site Inventory DDL."""

import re

from sqlalchemy import inspect, text


_SCHEMA_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}")


def resolve_postgres_schema(connection, schema: str | None) -> str:
    value = str(schema or "").strip()
    if not value:
        value = str(connection.execute(text("SELECT current_schema()")).scalar_one_or_none() or "").strip()
    if not _SCHEMA_RE.fullmatch(value):
        raise RuntimeError("Could not resolve a valid active PostgreSQL schema")
    if not inspect(connection).has_schema(value):
        raise RuntimeError(f"Target PostgreSQL schema does not exist: {value}")
    return value


def qualified(connection, schema: str, name: str) -> str:
    preparer = connection.dialect.identifier_preparer
    return f"{preparer.quote_schema(schema)}.{preparer.quote(name)}"


def require_table_columns(connection, schema: str, requirements: dict[str, set[str]]) -> None:
    inspector = inspect(connection)
    problems = []
    for table, expected in requirements.items():
        if not inspector.has_table(table, schema=schema):
            problems.append(f"missing table {table}")
            continue
        actual = {column["name"] for column in inspector.get_columns(table, schema=schema)}
        missing = expected - actual
        if missing:
            problems.append(f"{table} missing columns {sorted(missing)}")
        primary_key = set(inspector.get_pk_constraint(table, schema=schema).get("constrained_columns") or ())
        if "id" in expected and "id" not in primary_key:
            problems.append(f"{table}.id is not a primary-key column")
    if problems:
        raise RuntimeError("Site Inventory migration preflight failed: " + "; ".join(problems))


def ensure_version_table(connection, schema: str) -> None:
    inspector = inspect(connection)
    if inspector.has_table("site_inventory_schema_versions", schema=schema):
        columns = {column["name"] for column in inspector.get_columns("site_inventory_schema_versions", schema=schema)}
        primary_key = set(inspector.get_pk_constraint("site_inventory_schema_versions", schema=schema).get("constrained_columns") or ())
        if columns != {"version", "applied_at"} or primary_key != {"version"}:
            raise RuntimeError("Unexpected site_inventory_schema_versions structure; migration stopped")
        return
    target = qualified(connection, schema, "site_inventory_schema_versions")
    connection.exec_driver_sql(
        f"CREATE TABLE {target} (version VARCHAR(100) PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
    )


def version_applied(connection, schema: str, version: str) -> bool:
    target = qualified(connection, schema, "site_inventory_schema_versions")
    return connection.execute(
        text(f"SELECT 1 FROM {target} WHERE version=:version"), {"version": version}
    ).first() is not None


def record_version(connection, schema: str, version: str) -> None:
    target = qualified(connection, schema, "site_inventory_schema_versions")
    connection.execute(text(f"INSERT INTO {target} (version) VALUES (:version)"), {"version": version})


def validate_model_table(connection, schema: str, table) -> None:
    inspector = inspect(connection)
    name = table.name
    if not inspector.has_table(name, schema=schema):
        raise RuntimeError(f"Versioned Site Inventory schema is missing {name}")
    actual_columns = {column["name"] for column in inspector.get_columns(name, schema=schema)}
    expected_columns = {column.name for column in table.columns}
    if actual_columns != expected_columns:
        raise RuntimeError(
            f"Unexpected columns in {schema}.{name}; missing={sorted(expected_columns - actual_columns)}, "
            f"extra={sorted(actual_columns - expected_columns)}"
        )

    expected_pk = {column.name for column in table.primary_key.columns}
    actual_pk = set(inspector.get_pk_constraint(name, schema=schema).get("constrained_columns") or ())
    if actual_pk != expected_pk:
        raise RuntimeError(f"Unexpected primary key on {schema}.{name}")

    actual_constraint_names = set()
    for getter in (inspector.get_unique_constraints, inspector.get_check_constraints, inspector.get_foreign_keys):
        actual_constraint_names.update(
            constraint.get("name") for constraint in getter(name, schema=schema) if constraint.get("name")
        )
    expected_constraint_names = {
        constraint.name for constraint in table.constraints if constraint.name
    }
    missing_constraints = expected_constraint_names - actual_constraint_names
    if missing_constraints:
        actual_index_names = {index.get("name") for index in inspector.get_indexes(name, schema=schema)}
        missing_constraints -= actual_index_names
    if missing_constraints:
        raise RuntimeError(f"Missing constraints on {schema}.{name}: {sorted(missing_constraints)}")

    actual_indexes = {index.get("name") for index in inspector.get_indexes(name, schema=schema)}
    expected_indexes = {index.name for index in table.indexes if index.name}
    if expected_indexes - actual_indexes:
        raise RuntimeError(f"Missing indexes on {schema}.{name}: {sorted(expected_indexes - actual_indexes)}")
