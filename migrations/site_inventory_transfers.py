"""Additive, explicit Site-to-Site transfer migration.

This migration is intended for isolated test schemas only until a separately
reviewed production cutover is approved.
"""

import re
from datetime import datetime, timezone

from sqlalchemy import inspect, text

VERSION = "site-inventory-transfers-v1"
TRANSFER_TABLES = (
    "site_transfers",
    "site_transfer_items",
    "site_transfer_dispatches",
    "site_transfer_receipts",
    "site_transfer_receipt_items",
    "site_transfer_corrections",
)
REQUIRED_TABLES = (
    "sites",
    "products",
    "audit_logs",
    "site_balances",
    "site_material_movements",
    "site_inventory_schema_versions",
)


def _validate_schema(schema: str, dialect: str) -> str | None:
    if dialect == "sqlite":
        if schema in (None, "", "main"):
            return None
        raise ValueError("SQLite transfer migrations use the default isolated test database schema")
    value = str(schema or "").strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", value):
        raise ValueError("An explicit valid PostgreSQL schema name is required")
    if value.casefold() == "public":
        raise ValueError("Site transfer migrations cannot target the public schema")
    return value


def _qualified(connection, schema: str, table: str) -> str:
    preparer = connection.dialect.identifier_preparer
    return f"{preparer.quote_schema(schema)}.{preparer.quote(table)}"


def _version_applied(connection, schema: str | None) -> bool:
    if connection.dialect.name == "postgresql":
        qualified = _qualified(connection, schema, "site_inventory_schema_versions")
        return connection.execute(
            text(f"SELECT 1 FROM {qualified} WHERE version=:version"), {"version": VERSION}
        ).first() is not None
    return connection.execute(
        text("SELECT 1 FROM site_inventory_schema_versions WHERE version=:version"), {"version": VERSION}
    ).first() is not None


def _record_version(connection, schema: str | None) -> None:
    if connection.dialect.name == "postgresql":
        qualified = _qualified(connection, schema, "site_inventory_schema_versions")
        connection.execute(text(f"INSERT INTO {qualified} (version) VALUES (:version)"), {"version": VERSION})
    else:
        connection.execute(
            text("INSERT INTO site_inventory_schema_versions (version, applied_at) VALUES (:version, :at)"),
            {"version": VERSION, "at": datetime.now(timezone.utc).isoformat()},
        )


def _install_postgres_guards(connection, schema: str) -> None:
    function = _qualified(connection, schema, "guard_site_transfer_history")
    transfers = _qualified(connection, schema, "site_transfers")
    items = _qualified(connection, schema, "site_transfer_items")
    dispatches = _qualified(connection, schema, "site_transfer_dispatches")
    receipts = _qualified(connection, schema, "site_transfer_receipts")
    receipt_items = _qualified(connection, schema, "site_transfer_receipt_items")
    corrections = _qualified(connection, schema, "site_transfer_corrections")
    connection.exec_driver_sql(
        f"CREATE OR REPLACE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql AS $$ "
        "BEGIN "
        "IF TG_TABLE_NAME IN ('site_transfer_dispatches', 'site_transfer_receipts', "
        "'site_transfer_receipt_items', 'site_transfer_corrections') THEN "
        "RAISE EXCEPTION 'site transfer event history is append-only'; END IF; "
        "IF TG_TABLE_NAME = 'site_transfer_items' THEN "
        f"IF EXISTS (SELECT 1 FROM {transfers} t WHERE t.id=OLD.transfer_id AND t.status <> 'draft') THEN "
        "RAISE EXCEPTION 'transfer lines are immutable after draft'; END IF; "
        "IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF; END IF; "
        "IF TG_TABLE_NAME = 'site_transfers' THEN "
        "IF TG_OP = 'DELETE' THEN "
        "IF OLD.status <> 'draft' OR EXISTS ("
        f"SELECT 1 FROM {dispatches} d WHERE d.transfer_id=OLD.id) THEN "
        "RAISE EXCEPTION 'dispatched transfer history cannot be deleted'; END IF; "
        "RETURN OLD; END IF; "
        "IF OLD.program IS DISTINCT FROM NEW.program OR OLD.from_site_id IS DISTINCT FROM NEW.from_site_id "
        "OR OLD.to_site_id IS DISTINCT FROM NEW.to_site_id OR OLD.reference IS DISTINCT FROM NEW.reference "
        "OR OLD.notes IS DISTINCT FROM NEW.notes OR OLD.created_by IS DISTINCT FROM NEW.created_by "
        "OR OLD.request_token_hash IS DISTINCT FROM NEW.request_token_hash THEN "
        "RAISE EXCEPTION 'transfer identity and lines are immutable'; END IF; "
        "IF OLD.status IS DISTINCT FROM NEW.status AND NOT ("
        "(OLD.status='draft' AND NEW.status='approved') OR "
        "(OLD.status='approved' AND NEW.status='in_transit') OR "
        "(OLD.status='in_transit' AND NEW.status IN ('partially_received','received','cancelled')) OR "
        "(OLD.status='partially_received' AND NEW.status IN ('partially_received','received','in_transit')) OR "
        "(OLD.status='received' AND NEW.status IN ('partially_received','in_transit'))) THEN "
        "RAISE EXCEPTION 'invalid site transfer state transition'; END IF; "
        "RETURN NEW; END IF; "
        "RETURN NEW; END; $$"
    )
    for table, trigger, operations in (
        (transfers, "tr_site_transfer_guard", "UPDATE OR DELETE"),
        (items, "tr_site_transfer_item_guard", "UPDATE OR DELETE"),
        (dispatches, "tr_site_transfer_dispatch_append_only", "UPDATE OR DELETE"),
        (receipts, "tr_site_transfer_receipt_append_only", "UPDATE OR DELETE"),
        (receipt_items, "tr_site_transfer_receipt_item_append_only", "UPDATE OR DELETE"),
        (corrections, "tr_site_transfer_correction_append_only", "UPDATE OR DELETE"),
    ):
        quoted_trigger = connection.dialect.identifier_preparer.quote(trigger)
        connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS {quoted_trigger} ON {table}")
        connection.exec_driver_sql(
            f"CREATE TRIGGER {quoted_trigger} BEFORE {operations} ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION " + function + "()"
        )

    for table_name in TRANSFER_TABLES:
        qualified = _qualified(connection, schema, table_name)
        connection.exec_driver_sql(f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY")
        for role in ("anon", "authenticated"):
            exists = connection.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname=:role"), {"role": role}
            ).first()
            if exists:
                role_name = connection.dialect.identifier_preparer.quote(role)
                connection.exec_driver_sql(f"REVOKE ALL ON TABLE {qualified} FROM {role_name}")


def _install_sqlite_guards(connection) -> None:
    connection.exec_driver_sql(
        "CREATE TRIGGER IF NOT EXISTS tr_site_transfer_event_append_only "
        "BEFORE UPDATE ON site_transfer_dispatches BEGIN "
        "SELECT RAISE(ABORT, 'site transfer event history is append-only'); END"
    )
    for table in ("site_transfer_dispatches", "site_transfer_receipts", "site_transfer_receipt_items", "site_transfer_corrections"):
        connection.exec_driver_sql(
            f"CREATE TRIGGER IF NOT EXISTS tr_{table}_no_delete BEFORE DELETE ON {table} BEGIN "
            "SELECT RAISE(ABORT, 'site transfer event history is append-only'); END"
        )
        connection.exec_driver_sql(
            f"CREATE TRIGGER IF NOT EXISTS tr_{table}_no_update BEFORE UPDATE ON {table} BEGIN "
            "SELECT RAISE(ABORT, 'site transfer event history is append-only'); END"
        )
    connection.exec_driver_sql(
        "CREATE TRIGGER IF NOT EXISTS tr_site_transfer_items_after_draft "
        "BEFORE UPDATE ON site_transfer_items WHEN EXISTS ("
        "SELECT 1 FROM site_transfers WHERE id=OLD.transfer_id AND status <> 'draft') BEGIN "
        "SELECT RAISE(ABORT, 'transfer lines are immutable after draft'); END"
    )
    connection.exec_driver_sql(
        "CREATE TRIGGER IF NOT EXISTS tr_site_transfer_items_after_draft_delete "
        "BEFORE DELETE ON site_transfer_items WHEN EXISTS ("
        "SELECT 1 FROM site_transfers WHERE id=OLD.transfer_id AND status <> 'draft') BEGIN "
        "SELECT RAISE(ABORT, 'transfer lines are immutable after draft'); END"
    )
    connection.exec_driver_sql(
        "CREATE TRIGGER IF NOT EXISTS tr_site_transfer_history_delete "
        "BEFORE DELETE ON site_transfers WHEN OLD.status <> 'draft' BEGIN "
        "SELECT RAISE(ABORT, 'dispatched transfer history cannot be deleted'); END"
    )


def migrate(bind, schema: str) -> dict:
    """Create transfer tables only in an explicitly supplied isolated schema."""
    from models import (
        SiteTransfer,
        SiteTransferCorrection,
        SiteTransferDispatch,
        SiteTransferItem,
        SiteTransferReceipt,
        SiteTransferReceiptItem,
    )

    dialect = bind.dialect.name
    schema_name = _validate_schema(schema, dialect)
    if dialect not in {"postgresql", "sqlite"}:
        raise RuntimeError(f"Unsupported transfer migration dialect: {dialect}")

    with bind.connect() as connection:
        transaction = connection.begin()
        try:
            if dialect == "postgresql":
                connection.exec_driver_sql(
                    f"CREATE SCHEMA IF NOT EXISTS {connection.dialect.identifier_preparer.quote_schema(schema_name)}"
                )
                translated = connection.execution_options(schema_translate_map={None: schema_name})
                missing = [table for table in REQUIRED_TABLES if not inspect(connection).has_table(table, schema=schema_name)]
                if missing:
                    raise RuntimeError("Site Inventory Foundation is required in the transfer schema first")
                version_table = _qualified(connection, schema_name, "site_inventory_schema_versions")
                if connection.execute(text(f"SELECT 1 FROM {version_table} WHERE version='site-inventory-foundation-v1'")).first() is None:
                    raise RuntimeError("Site Inventory Foundation migration version is not present in this schema")
                if _version_applied(connection, schema_name):
                    transaction.commit()
                    return {"version": VERSION, "schema": schema_name, "already_applied": True, "tables": list(TRANSFER_TABLES)}
                existing = [table for table in TRANSFER_TABLES if inspect(connection).has_table(table, schema=schema_name)]
                if existing:
                    raise RuntimeError("Partial unversioned Site Transfer schema found; migration stopped without guessing")
                ordered_tables = (
                    SiteTransfer.__table__,
                    SiteTransferItem.__table__,
                    SiteTransferDispatch.__table__,
                    SiteTransferReceipt.__table__,
                    SiteTransferReceiptItem.__table__,
                    SiteTransferCorrection.__table__,
                )
                for table in ordered_tables:
                    table.create(bind=translated, checkfirst=False)
                _install_postgres_guards(connection, schema_name)
                _record_version(connection, schema_name)
            else:
                missing = [table for table in REQUIRED_TABLES if not inspect(connection).has_table(table)]
                if missing:
                    raise RuntimeError("Site Inventory Foundation is required before transfer tables")
                if _version_applied(connection, None):
                    transaction.commit()
                    return {"version": VERSION, "schema": "main", "already_applied": True, "tables": list(TRANSFER_TABLES)}
                existing = [table for table in TRANSFER_TABLES if inspect(connection).has_table(table)]
                if existing:
                    raise RuntimeError("Partial unversioned SQLite Site Transfer schema found; migration stopped")
                for table in (
                    SiteTransfer.__table__, SiteTransferItem.__table__, SiteTransferDispatch.__table__,
                    SiteTransferReceipt.__table__, SiteTransferReceiptItem.__table__, SiteTransferCorrection.__table__,
                ):
                    table.create(bind=connection, checkfirst=False)
                _install_sqlite_guards(connection)
                _record_version(connection, None)
            transaction.commit()
        except Exception:
            transaction.rollback()
            raise

    return {"version": VERSION, "schema": schema_name or "main", "already_applied": False, "tables": list(TRANSFER_TABLES)}
