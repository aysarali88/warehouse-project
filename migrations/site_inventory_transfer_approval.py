"""Add assigned Requester approval records for Site-to-Site transfers."""

from sqlalchemy import inspect, text

from migrations.site_inventory_migration_utils import (
    ensure_version_table,
    qualified,
    record_version,
    require_table_columns,
    resolve_postgres_schema,
    validate_model_table,
    version_applied,
)

VERSION = "site-inventory-transfer-approval-v1"
PREVIOUS_VERSIONS = (
    "site-inventory-foundation-v1",
    "site-inventory-rollout-cutover-v1",
    "site-inventory-transfers-v1",
    "site-inventory-site-activation-v1",
    "site-inventory-negative-opening-v1",
)
TABLE = "site_transfer_approvals"


def _install_postgres_guard(connection, schema: str) -> None:
    table = qualified(connection, schema, TABLE)
    function = qualified(connection, schema, "guard_site_transfer_approval")
    trigger_name = connection.dialect.identifier_preparer.quote("tr_site_transfer_approval_immutable")
    existing_function = connection.execute(text(
        "SELECT pg_get_functiondef(p.oid) FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid=p.pronamespace "
        "WHERE n.nspname=:schema AND p.proname='guard_site_transfer_approval' AND p.pronargs=0"
    ), {"schema": schema}).scalar_one_or_none()
    marker = "site transfer approval history is immutable"
    if existing_function and marker not in existing_function:
        raise RuntimeError("Unexpected transfer approval guard function; migration stopped")
    if not existing_function:
        connection.exec_driver_sql(
            f"CREATE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN "
            "IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'site transfer approval history is immutable'; END IF; "
            "IF OLD.status <> 'pending' OR NEW.status NOT IN ('approved', 'rejected') "
            "OR NEW.program <> OLD.program OR NEW.transfer_id <> OLD.transfer_id "
            "OR NEW.created_by_user_id <> OLD.created_by_user_id "
            "OR NEW.approver_user_id <> OLD.approver_user_id OR NEW.created_at <> OLD.created_at "
            "OR NEW.decided_by_user_id IS NULL OR NEW.decided_by_user_id <> OLD.approver_user_id "
            "OR NEW.decided_at IS NULL THEN "
            "RAISE EXCEPTION 'site transfer approval history is immutable'; END IF; "
            "RETURN NEW; END; $$"
        )
    trigger = connection.execute(text(
        "SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=:schema AND c.relname=:table "
        "AND t.tgname='tr_site_transfer_approval_immutable' AND NOT t.tgisinternal"
    ), {"schema": schema, "table": TABLE}).first()
    if not trigger:
        connection.exec_driver_sql(
            f"CREATE TRIGGER {trigger_name} BEFORE UPDATE OR DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION {function}()"
        )
    connection.exec_driver_sql(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    connection.exec_driver_sql(f"REVOKE ALL ON TABLE {table} FROM PUBLIC")
    for role in ("anon", "authenticated"):
        if connection.execute(text("SELECT 1 FROM pg_roles WHERE rolname=:role"), {"role": role}).first():
            role_name = connection.dialect.identifier_preparer.quote(role)
            connection.exec_driver_sql(f"REVOKE ALL ON TABLE {table} FROM {role_name}")


def _install_sqlite_guard(connection) -> None:
    connection.exec_driver_sql(
        "CREATE TRIGGER IF NOT EXISTS tr_site_transfer_approval_no_delete "
        f"BEFORE DELETE ON {TABLE} BEGIN "
        "SELECT RAISE(ABORT, 'site transfer approval history is immutable'); END"
    )
    connection.exec_driver_sql(
        "CREATE TRIGGER IF NOT EXISTS tr_site_transfer_approval_transition "
        f"BEFORE UPDATE ON {TABLE} WHEN OLD.status <> 'pending' "
        "OR NEW.status NOT IN ('approved', 'rejected') "
        "OR NEW.program <> OLD.program OR NEW.transfer_id <> OLD.transfer_id "
        "OR NEW.created_by_user_id <> OLD.created_by_user_id "
        "OR NEW.approver_user_id <> OLD.approver_user_id "
        "OR NEW.created_at <> OLD.created_at "
        "OR NEW.decided_by_user_id IS NULL OR NEW.decided_by_user_id <> OLD.approver_user_id "
        "OR NEW.decided_at IS NULL BEGIN "
        "SELECT RAISE(ABORT, 'site transfer approval history is immutable'); END"
    )


def migrate(bind, schema: str | None = None) -> dict:
    """Create the approval assignment/history table without changing transfers."""
    from models import SiteTransfer, SiteTransferApproval

    dialect = bind.dialect.name
    if dialect not in {"postgresql", "sqlite"}:
        raise RuntimeError(f"Unsupported transfer approval migration dialect: {dialect}")
    schema_name = schema or ("main" if dialect == "sqlite" else None)

    with bind.connect() as connection:
        transaction = connection.begin()
        try:
            if dialect == "postgresql":
                schema_name = resolve_postgres_schema(connection, schema)
                require_table_columns(connection, schema_name, {
                    "site_inventory_schema_versions": {"version", "applied_at"},
                    "site_transfers": {"id", "program", "status", "created_by"},
                    "app_users": {"id", "program", "username", "name", "role", "status"},
                })
                ensure_version_table(connection, schema_name)
                validate_model_table(connection, schema_name, SiteTransfer.__table__)
                versions_table = qualified(connection, schema_name, "site_inventory_schema_versions")
                recorded = set(connection.execute(text(f"SELECT version FROM {versions_table}")).scalars())
                already_applied = version_applied(connection, schema_name, VERSION)
                expected = set(PREVIOUS_VERSIONS) | ({VERSION} if already_applied else set())
                if recorded != expected:
                    raise RuntimeError("Transfer approval migration requires all prior Site Inventory migrations")
                target_exists = inspect(connection).has_table(TABLE, schema=schema_name)
                if already_applied:
                    if not target_exists:
                        raise RuntimeError("Recorded transfer approval migration is missing its table")
                    validate_model_table(connection, schema_name, SiteTransferApproval.__table__)
                    _install_postgres_guard(connection, schema_name)
                    transaction.commit()
                    return {"version": VERSION, "schema": schema_name, "already_applied": True, "table": TABLE}
                if target_exists:
                    raise RuntimeError("Unversioned transfer approval table found; migration stopped")
                translated = connection.execution_options(schema_translate_map={None: schema_name})
                SiteTransferApproval.__table__.create(bind=translated, checkfirst=False)
                validate_model_table(connection, schema_name, SiteTransferApproval.__table__)
                _install_postgres_guard(connection, schema_name)
                record_version(connection, schema_name, VERSION)
            else:
                inspector = inspect(connection)
                require_table_columns(connection, "main", {
                    "site_inventory_schema_versions": {"version", "applied_at"},
                    "site_transfers": {"id", "program", "status", "created_by"},
                    "app_users": {"id", "program", "username", "name", "role", "status"},
                })
                recorded = set(connection.execute(text(
                    "SELECT version FROM site_inventory_schema_versions"
                )).scalars())
                already_applied = connection.execute(text(
                    "SELECT 1 FROM site_inventory_schema_versions WHERE version=:version"
                ), {"version": VERSION}).first() is not None
                expected = set(PREVIOUS_VERSIONS) | ({VERSION} if already_applied else set())
                if recorded != expected:
                    raise RuntimeError("Transfer approval migration requires all prior Site Inventory migrations")
                target_exists = inspector.has_table(TABLE)
                if already_applied:
                    if not target_exists:
                        raise RuntimeError("Recorded transfer approval migration is missing its table")
                    validate_model_table(connection, None, SiteTransferApproval.__table__)
                    _install_sqlite_guard(connection)
                    transaction.commit()
                    return {"version": VERSION, "schema": "main", "already_applied": True, "table": TABLE}
                if target_exists:
                    raise RuntimeError("Unversioned transfer approval table found; migration stopped")
                SiteTransferApproval.__table__.create(bind=connection, checkfirst=False)
                validate_model_table(connection, None, SiteTransferApproval.__table__)
                _install_sqlite_guard(connection)
                connection.execute(text(
                    "INSERT INTO site_inventory_schema_versions (version, applied_at) "
                    "VALUES (:version, CURRENT_TIMESTAMP)"
                ), {"version": VERSION})

            transaction.commit()
        except Exception:
            transaction.rollback()
            raise

    return {"version": VERSION, "schema": schema_name or "main", "already_applied": False, "table": TABLE}
