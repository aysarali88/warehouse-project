"""Permit signed initial opening balances while retaining service-level debit controls."""

import re

from sqlalchemy import inspect, text

from migrations.site_inventory_migration_utils import (
    qualified,
    record_version,
    require_table_columns,
    resolve_postgres_schema,
)

VERSION = "site-inventory-negative-opening-v1"
PREVIOUS_VERSIONS = (
    "site-inventory-foundation-v1",
    "site-inventory-rollout-cutover-v1",
    "site-inventory-transfers-v1",
    "site-inventory-site-activation-v1",
)
LATER_VERSIONS = ("site-inventory-transfer-approval-v1",)
LEGACY_CHECKS = {
    "site_balances": ("ck_site_balance_nonnegative", "on_hand_qty"),
    "site_opening_balance_requests": ("ck_site_opening_quantity_nonnegative", "quantity"),
}


def _legacy_checks(connection, schema: str | None) -> set[str]:
    inspector = inspect(connection)
    present = set()
    for table, (name, column) in LEGACY_CHECKS.items():
        checks = {row["name"]: row for row in inspector.get_check_constraints(table, schema=schema)}
        unexpected = set(checks) - {name}
        if unexpected:
            raise RuntimeError(
                f"Unexpected CHECK constraints on {table}: {sorted(unexpected)}; migration stopped"
            )
        if name not in checks:
            continue
        definition = re.sub(r"[\s()]", "", str(checks[name].get("sqltext") or "").lower())
        if definition not in {f"{column}>=0", f"{column}>=0::numeric"}:
            raise RuntimeError(f"Unexpected definition for {table}.{name}; migration stopped")
        present.add(name)
    return present


def migrate(bind, schema: str | None = None) -> dict:
    """Remove only the two legacy nonnegative checks before the first cutover."""
    if bind.dialect.name not in {"postgresql", "sqlite"}:
        raise RuntimeError("Unsupported Site Inventory migration dialect")
    with bind.connect() as connection:
        transaction = connection.begin()
        try:
            schema_name = resolve_postgres_schema(connection, schema) if bind.dialect.name == "postgresql" else None
            if bind.dialect.name == "sqlite" and schema not in (None, "", "main"):
                raise RuntimeError("SQLite migration requires its isolated main schema")
            lookup_schema = schema_name or "main"
            require_table_columns(connection, lookup_schema, {
                "site_inventory_schema_versions": {"version", "applied_at"},
                "site_balances": {"on_hand_qty", "site_id", "product_id"},
                "site_opening_balance_requests": {"quantity", "site_id", "product_id"},
                "site_material_movements": {"quantity_delta", "site_id", "product_id"},
            })
            version_table = qualified(connection, schema_name, "site_inventory_schema_versions") if schema_name else "site_inventory_schema_versions"
            if bind.dialect.name == "postgresql":
                lock_targets = (
                    version_table,
                    qualified(connection, schema_name, "site_opening_balance_requests"),
                    qualified(connection, schema_name, "site_material_movements"),
                    qualified(connection, schema_name, "site_balances"),
                )
                connection.exec_driver_sql(
                    f"LOCK TABLE {', '.join(lock_targets)} IN ACCESS EXCLUSIVE MODE"
                )
            versions = set(connection.execute(text(f"SELECT version FROM {version_table}")).scalars())
            already_applied = VERSION in versions
            expected_before = set(PREVIOUS_VERSIONS)
            expected_after = expected_before | {VERSION}
            allowed = ({frozenset(expected_after), frozenset(expected_after | set(LATER_VERSIONS))}
                       if already_applied else {frozenset(expected_before)})
            if frozenset(versions) not in allowed:
                raise RuntimeError("Negative-opening migration requires exactly the four prior Site Inventory versions")
            present = _legacy_checks(connection, schema_name)
            all_legacy = {name for name, _ in LEGACY_CHECKS.values()}
            if present not in (set(), all_legacy):
                raise RuntimeError("Partial legacy nonnegative constraints found; migration stopped")
            if already_applied:
                if present:
                    raise RuntimeError("Recorded negative-opening migration still has legacy constraints")
                transaction.commit()
                return {"version": VERSION, "schema": lookup_schema, "already_applied": True}
            for table in ("site_balances", "site_opening_balance_requests", "site_material_movements"):
                target = qualified(connection, schema_name, table) if schema_name else table
                if connection.execute(text(f"SELECT 1 FROM {target} LIMIT 1")).first():
                    raise RuntimeError("Site Inventory cutover data already exists; migration stopped")
            if present:
                if bind.dialect.name != "postgresql":
                    raise RuntimeError("Legacy SQLite checks require an isolated database rebuild")
                for table, (constraint, _) in LEGACY_CHECKS.items():
                    target = qualified(connection, schema_name, table)
                    quoted_name = connection.dialect.identifier_preparer.quote(constraint)
                    connection.exec_driver_sql(f"ALTER TABLE {target} DROP CONSTRAINT {quoted_name}")
            if _legacy_checks(connection, schema_name):
                raise RuntimeError("Legacy nonnegative constraints remain after migration")
            if schema_name:
                record_version(connection, schema_name, VERSION)
            else:
                connection.execute(text(
                    "INSERT INTO site_inventory_schema_versions (version, applied_at) "
                    "VALUES (:version, CURRENT_TIMESTAMP)"
                ), {"version": VERSION})
            transaction.commit()
            return {"version": VERSION, "schema": lookup_schema, "already_applied": False}
        except Exception:
            transaction.rollback()
            raise
