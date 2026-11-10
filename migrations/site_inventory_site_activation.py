"""Add the fail-closed per-Site Site Inventory activation flag."""

from sqlalchemy import Boolean, inspect, text

from migrations.site_inventory_migration_utils import (
    ensure_version_table,
    qualified,
    record_version,
    require_table_columns,
    resolve_postgres_schema,
    version_applied,
)

VERSION = "site-inventory-site-activation-v1"
COLUMN = "site_inventory_enabled"
PREVIOUS_VERSIONS = (
    "site-inventory-foundation-v1",
    "site-inventory-rollout-cutover-v1",
    "site-inventory-transfers-v1",
)


def _column_definition(connection, schema: str | None) -> dict | None:
    columns = inspect(connection).get_columns("sites", schema=schema)
    return next((column for column in columns if column["name"] == COLUMN), None)


def _default_is_false(value) -> bool:
    normalized = str(value or "").strip().lower().replace("(", "").replace(")", "")
    return normalized in {"false", "false::boolean", "0", "0::boolean", "false::bool"}


def _validate_column(connection, schema: str | None) -> bool:
    column = _column_definition(connection, schema)
    if column is None:
        return False
    if connection.dialect.name == "postgresql" and not isinstance(column["type"], Boolean):
        raise RuntimeError("sites.site_inventory_enabled has an unexpected type")
    if column.get("nullable", True) or not _default_is_false(column.get("default")):
        raise RuntimeError("sites.site_inventory_enabled must be NOT NULL DEFAULT FALSE")
    return True


def _check_prior_versions(connection, schema: str | None, *, activation_applied: bool) -> None:
    inspector = inspect(connection)
    if not inspector.has_table("site_inventory_schema_versions", schema=schema):
        raise RuntimeError("Site Inventory Foundation migration is required before site activation")
    columns = {column["name"] for column in inspector.get_columns("site_inventory_schema_versions", schema=schema)}
    primary_key = set(inspector.get_pk_constraint(
        "site_inventory_schema_versions", schema=schema
    ).get("constrained_columns") or ())
    if columns != {"version", "applied_at"} or primary_key != {"version"}:
        raise RuntimeError("Unexpected Site Inventory migration version table")
    table = qualified(connection, schema, "site_inventory_schema_versions") if schema else "site_inventory_schema_versions"
    recorded = set(connection.execute(text(f"SELECT version FROM {table}")).scalars())
    expected = set(PREVIOUS_VERSIONS)
    if activation_applied:
        expected.add(VERSION)
    if recorded != expected:
        raise RuntimeError("Site activation migration requires the complete expected prior migration state")


def migrate(bind, schema: str | None = None) -> dict:
    """Add a false-by-default activation flag without updating existing Site rows."""
    dialect = bind.dialect.name
    if dialect not in {"postgresql", "sqlite"}:
        raise RuntimeError(f"Unsupported site activation migration dialect: {dialect}")

    with bind.connect() as connection:
        transaction = connection.begin()
        try:
            if dialect == "postgresql":
                schema_name = resolve_postgres_schema(connection, schema)
                require_table_columns(connection, schema_name, {
                    "sites": {"id", "program", "name"},
                    "site_inventory_schema_versions": {"version", "applied_at"},
                })
                ensure_version_table(connection, schema_name)
            else:
                schema_name = None
                require_table_columns(connection, "main", {
                    "sites": {"id", "program", "name"},
                    "site_inventory_schema_versions": {"version", "applied_at"},
                })

            applied = version_applied(connection, schema_name or "main", VERSION) if dialect == "postgresql" else (
                connection.execute(text(
                    "SELECT 1 FROM site_inventory_schema_versions WHERE version=:version"
                ), {"version": VERSION}).first() is not None
            )
            _check_prior_versions(connection, schema_name, activation_applied=applied)
            has_column = _validate_column(connection, schema_name)
            if applied:
                if not has_column:
                    raise RuntimeError("Recorded site activation migration is missing its column")
                transaction.commit()
                return {"version": VERSION, "schema": schema_name or "main", "already_applied": True}
            if has_column:
                raise RuntimeError("Unversioned sites.site_inventory_enabled column found; migration stopped")

            target = qualified(connection, schema_name, "sites") if schema_name else "sites"
            default = "FALSE" if dialect == "postgresql" else "0"
            connection.exec_driver_sql(
                f"ALTER TABLE {target} ADD COLUMN {COLUMN} BOOLEAN NOT NULL DEFAULT {default}"
            )
            if not _validate_column(connection, schema_name):
                raise RuntimeError("Site activation column verification failed")
            if dialect == "postgresql":
                record_version(connection, schema_name, VERSION)
            else:
                connection.execute(text(
                    "INSERT INTO site_inventory_schema_versions (version, applied_at) "
                    "VALUES (:version, CURRENT_TIMESTAMP)"
                ), {"version": VERSION})
            transaction.commit()
        except Exception:
            transaction.rollback()
            raise

    return {"version": VERSION, "schema": schema_name or "main", "already_applied": False}
