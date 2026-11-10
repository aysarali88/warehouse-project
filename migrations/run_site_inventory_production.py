"""Explicit, fail-closed runner for the approved Production migrations.

Running without --execute performs a read-only preflight and makes no changes.
No dotenv loading or database URL fallback is performed here.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from urllib.parse import unquote

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from migrations.site_inventory_foundation import FOUNDATION_TABLES, VERSION as FOUNDATION_VERSION, migrate as migrate_foundation
from migrations.site_inventory_rollout_cutover import COLUMN as ROLLOUT_CUTOFF_COLUMN
from migrations.site_inventory_rollout_cutover import VERSION as ROLLOUT_VERSION, migrate as migrate_rollout_cutover
from migrations.site_inventory_transfers import TRANSFER_TABLES, VERSION as TRANSFERS_VERSION, migrate as migrate_transfers
from migrations.site_inventory_site_activation import COLUMN as SITE_ACTIVATION_COLUMN
from migrations.site_inventory_site_activation import VERSION as SITE_ACTIVATION_VERSION, migrate as migrate_site_activation
from migrations.site_inventory_negative_opening import LEGACY_CHECKS
from migrations.site_inventory_negative_opening import VERSION as NEGATIVE_OPENING_VERSION, _legacy_checks, migrate as migrate_negative_opening
from migrations.site_inventory_migration_utils import qualified, validate_model_table


PRODUCTION_PROJECT_REF = "ndhccmrxpujpqhyewoac"
WAREHOUSE_TEST_PROJECT_REF = "kdzhcizesmjseadwegvd"
MIGRATION_ORDER = (FOUNDATION_VERSION, ROLLOUT_VERSION, TRANSFERS_VERSION, SITE_ACTIVATION_VERSION, NEGATIVE_OPENING_VERSION)
MIGRATION_SPECS = (
    (FOUNDATION_VERSION, migrate_foundation),
    (ROLLOUT_VERSION, migrate_rollout_cutover),
    (TRANSFERS_VERSION, migrate_transfers),
    (SITE_ACTIVATION_VERSION, migrate_site_activation),
    (NEGATIVE_OPENING_VERSION, migrate_negative_opening),
)
FOUNDATION_TABLE_SET = frozenset(FOUNDATION_TABLES)
TRANSFER_TABLE_SET = frozenset(TRANSFER_TABLES)
MANAGED_TABLES = FOUNDATION_TABLE_SET | TRANSFER_TABLE_SET
REQUIRED_COLUMNS = {
    "sites": {"id", "program", "name"},
    "products": {"id", "program", "unit"},
    "audit_logs": {"id", "action", "entity_type", "entity_id", "actor", "details", "created_at"},
    "rollout_records": {"id", "record_id", "date", "entry_time", "status", "area"},
}
ALLOWED_DATABASE_URL_ENV_NAMES = frozenset({
    "DATABASE_URL",
    "FTTH_DATABASE_URL",
    "SITE_INVENTORY_PRODUCTION_DATABASE_URL",
})
_SCHEMA_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}")
_ADVISORY_LOCK_KEY = 74130495027642


class MigrationPreflightError(RuntimeError):
    """A safe, user-actionable reason to stop before applying migrations."""


def resolve_explicit_database_url(env_name: str, environ: dict[str, str]) -> str:
    if env_name == "TEST_DATABASE_URL" or env_name not in ALLOWED_DATABASE_URL_ENV_NAMES:
        raise MigrationPreflightError("Choose an allowed explicit Production database URL environment variable")
    value = str(environ.get(env_name, "") or "").strip()
    if not value:
        raise MigrationPreflightError(f"Explicit database URL environment variable is empty: {env_name}")
    return value


def production_url_identity(database_url: str) -> dict:
    """Parse only safe target identity fields; never return or print credentials."""
    try:
        url = make_url(str(database_url or "").strip())
        username = unquote(url.username or "")
        host = (url.host or "").lower().rstrip(".")
        port = int(url.port or 5432)
        database = unquote(url.database or "").strip("/")
    except Exception as exc:
        raise MigrationPreflightError("Database URL could not be parsed; credentials were not displayed") from exc

    if url.get_backend_name() != "postgresql":
        raise MigrationPreflightError("The explicit Production URL must use PostgreSQL")

    safe_identity_fields = f"{host} {username} {database}".casefold()
    test_ref_detected = WAREHOUSE_TEST_PROJECT_REF in safe_identity_fields
    pooler_match = (
        host.endswith(".pooler.supabase.com")
        and port in {5432, 6543}
        and username.casefold() == f"postgres.{PRODUCTION_PROJECT_REF}"
    )
    direct_match = (
        host == f"db.{PRODUCTION_PROJECT_REF}.supabase.co"
        and username.casefold() == "postgres"
        and port == 5432
    )
    project_ref_match = pooler_match or direct_match
    identity = {
        "host": host,
        "port": port,
        "database": database,
        "project_ref_match": project_ref_match,
        "production_project_ref_detected": PRODUCTION_PROJECT_REF in host or PRODUCTION_PROJECT_REF in username.casefold(),
        "test_project_ref_detected": test_ref_detected,
        "connection_mode": "Supabase Pooler" if pooler_match else "Supabase direct PostgreSQL" if direct_match else "unverified",
    }
    if test_ref_detected:
        raise MigrationPreflightError("Warehouse-Test identity detected; Production migration runner refused the connection")
    if not project_ref_match or not identity["production_project_ref_detected"]:
        raise MigrationPreflightError("The explicit URL does not identify the approved Production project")
    if database != "postgres":
        raise MigrationPreflightError("The approved Production database name must be postgres")
    return identity


def validate_target_identity(database_name: str, active_schema: str, target_schema: str) -> None:
    if database_name != "postgres":
        raise MigrationPreflightError("Connected database name is not postgres")
    if not _SCHEMA_RE.fullmatch(str(target_schema or "")):
        raise MigrationPreflightError("Target schema name is invalid")
    if active_schema != target_schema:
        raise MigrationPreflightError("Connected active schema does not match the explicit target schema")


def validate_schema_prerequisites(available_columns: dict[str, set[str]]) -> None:
    problems = []
    for table, required in REQUIRED_COLUMNS.items():
        actual = available_columns.get(table)
        if actual is None:
            problems.append(f"missing prerequisite table {table}")
            continue
        missing = required - set(actual)
        if missing:
            problems.append(f"{table} missing columns {sorted(missing)}")
    if problems:
        raise MigrationPreflightError("Production schema prerequisites failed: " + "; ".join(problems))


def validate_migration_state(
    *,
    version_table_exists: bool,
    version_columns: set[str] | None,
    version_primary_key: set[str] | None,
    recorded_versions: list[str],
    existing_managed_tables: set[str],
    has_rollout_cutover_column: bool,
    has_site_activation_column: bool,
    negative_opening_check_state: str = "legacy",
) -> str:
    if not version_table_exists:
        if recorded_versions or existing_managed_tables or has_rollout_cutover_column or has_site_activation_column:
            raise MigrationPreflightError("Unversioned or partial Site Inventory schema detected; no migration was run")
        return "clean"

    if version_columns != {"version", "applied_at"} or version_primary_key != {"version"}:
        raise MigrationPreflightError("Unexpected site_inventory_schema_versions structure")
    if not recorded_versions:
        raise MigrationPreflightError("Empty migration version table is an unexpected partial state")
    if len(set(recorded_versions)) != len(recorded_versions):
        raise MigrationPreflightError("Duplicate migration version records detected")
    if recorded_versions != list(MIGRATION_ORDER[:len(recorded_versions)]):
        raise MigrationPreflightError("Migration versions are unknown, out of order, or have a gap")

    applied_count = len(recorded_versions)
    expected_foundation = set(FOUNDATION_TABLE_SET) if applied_count >= 1 else set()
    expected_transfers = set(TRANSFER_TABLE_SET) if applied_count >= 3 else set()
    actual_foundation = existing_managed_tables & FOUNDATION_TABLE_SET
    actual_transfers = existing_managed_tables & TRANSFER_TABLE_SET
    if actual_foundation != expected_foundation:
        raise MigrationPreflightError("Foundation migration version does not match its table state")
    if actual_transfers != expected_transfers:
        raise MigrationPreflightError("Transfer migration version does not match its table state")
    if has_rollout_cutover_column != (applied_count >= 2):
        raise MigrationPreflightError("Rollout cutover version does not match its column state")
    if has_site_activation_column != (applied_count >= 4):
        raise MigrationPreflightError("Site activation version does not match its column state")
    if negative_opening_check_state == "partial":
        raise MigrationPreflightError("Partial negative-opening constraint state detected")
    if applied_count >= 5 and negative_opening_check_state != "relaxed":
        raise MigrationPreflightError("Negative-opening version does not match its constraint state")
    return "complete" if applied_count == len(MIGRATION_ORDER) else f"prefix-{applied_count}-of-{len(MIGRATION_ORDER)}"


def _version_table_security(connection, schema: str) -> dict:
    target = qualified(connection, schema, "site_inventory_schema_versions")
    row = connection.execute(text(
        "SELECT c.relrowsecurity, c.relforcerowsecurity, "
        "CASE WHEN EXISTS (SELECT 1 FROM pg_roles WHERE rolname='anon') THEN "
        "(has_table_privilege('anon', c.oid, 'SELECT') OR has_table_privilege('anon', c.oid, 'INSERT') "
        "OR has_table_privilege('anon', c.oid, 'UPDATE') OR has_table_privilege('anon', c.oid, 'DELETE') "
        "OR has_table_privilege('anon', c.oid, 'TRUNCATE') OR has_table_privilege('anon', c.oid, 'REFERENCES') "
        "OR has_table_privilege('anon', c.oid, 'TRIGGER')) ELSE false END, "
        "CASE WHEN EXISTS (SELECT 1 FROM pg_roles WHERE rolname='authenticated') THEN "
        "(has_table_privilege('authenticated', c.oid, 'SELECT') OR has_table_privilege('authenticated', c.oid, 'INSERT') "
        "OR has_table_privilege('authenticated', c.oid, 'UPDATE') OR has_table_privilege('authenticated', c.oid, 'DELETE') "
        "OR has_table_privilege('authenticated', c.oid, 'TRUNCATE') OR has_table_privilege('authenticated', c.oid, 'REFERENCES') "
        "OR has_table_privilege('authenticated', c.oid, 'TRIGGER')) ELSE false END, "
        "CASE WHEN EXISTS (SELECT 1 FROM pg_roles WHERE rolname='service_role') THEN "
        "(has_table_privilege('service_role', c.oid, 'SELECT') OR has_table_privilege('service_role', c.oid, 'INSERT') "
        "OR has_table_privilege('service_role', c.oid, 'UPDATE') OR has_table_privilege('service_role', c.oid, 'DELETE') "
        "OR has_table_privilege('service_role', c.oid, 'TRUNCATE') OR has_table_privilege('service_role', c.oid, 'REFERENCES') "
        "OR has_table_privilege('service_role', c.oid, 'TRIGGER')) ELSE false END, "
        "EXISTS (SELECT 1 FROM aclexplode(COALESCE(c.relacl, acldefault('r', c.relowner))) a "
        "WHERE a.grantee=0 AND a.privilege_type IN ('SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER')), "
        "has_table_privilege(current_user, c.oid, 'SELECT'), "
        "has_table_privilege(current_user, c.oid, 'INSERT'), "
        "c.relowner=(SELECT oid FROM pg_roles WHERE rolname=current_user), "
        "EXISTS (SELECT 1 FROM pg_roles WHERE rolname=current_user AND (rolsuper OR rolbypassrls)) "
        "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=:schema AND c.relname='site_inventory_schema_versions' AND c.relkind='r'"
    ), {"schema": schema}).first()
    if row is None:
        return {"exists": False, "protected": None, "server_access": None}
    (
        rls, force_rls, anon_access, auth_access, service_access, public_access,
        server_select, server_insert, server_owner, server_bypassrls,
    ) = row
    return {
        "exists": True,
        "rls_enabled": bool(rls),
        "force_rls": bool(force_rls),
        "anon_access": bool(anon_access),
        "authenticated_access": bool(auth_access),
        "service_role_access": bool(service_access),
        "public_access": bool(public_access),
        "server_access": bool(
            server_select and server_insert
            and (not rls or (server_owner and not force_rls) or server_bypassrls)
        ),
        "protected": bool(
            rls and not force_rls and not anon_access and not auth_access
            and not service_access and not public_access
        ),
    }


def preflight_database(engine, schema: str) -> dict:
    if engine.dialect.name != "postgresql":
        raise MigrationPreflightError("Production migration runner requires PostgreSQL")
    if not _SCHEMA_RE.fullmatch(str(schema or "")):
        raise MigrationPreflightError("Target schema name is invalid")

    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            row = connection.execute(text(
                "SELECT current_database(), current_schema(), current_setting('server_version_num')"
            )).one()
            database_name, active_schema, server_version_num = row
            validate_target_identity(database_name, active_schema, schema)
            inspector = inspect(connection)
            if not inspector.has_schema(schema):
                raise MigrationPreflightError("Explicit target schema does not exist")

            available_columns = {}
            for table in REQUIRED_COLUMNS:
                if inspector.has_table(table, schema=schema):
                    available_columns[table] = {
                        column["name"] for column in inspector.get_columns(table, schema=schema)
                    }
            validate_schema_prerequisites(available_columns)

            version_table_exists = inspector.has_table("site_inventory_schema_versions", schema=schema)
            version_columns = None
            version_primary_key = None
            recorded_versions = []
            security = {"exists": False, "protected": None, "server_access": None}
            if version_table_exists:
                version_columns = {
                    column["name"] for column in inspector.get_columns("site_inventory_schema_versions", schema=schema)
                }
                version_primary_key = set(
                    inspector.get_pk_constraint("site_inventory_schema_versions", schema=schema).get("constrained_columns") or ()
                )
                version_table = qualified(connection, schema, "site_inventory_schema_versions")
                recorded_versions = list(connection.execute(text(
                    f"SELECT version FROM {version_table} ORDER BY applied_at, version"
                )).scalars())
                security = _version_table_security(connection, schema)
                if security["force_rls"]:
                    raise MigrationPreflightError("Unexpected FORCE RLS on migration version table; migration stopped")
                if not security["server_access"]:
                    raise MigrationPreflightError("Migration version table is not accessible to the database server role")

            existing_managed = {
                name for name in MANAGED_TABLES if inspector.has_table(name, schema=schema)
            }
            rollout_columns = {
                column["name"] for column in inspector.get_columns("rollout_records", schema=schema)
            }
            site_columns = {
                column["name"] for column in inspector.get_columns("sites", schema=schema)
            }
            legacy_checks = _legacy_checks(connection, schema) if version_table_exists else set()
            expected_legacy_checks = {name for name, _ in LEGACY_CHECKS.values()}
            state = validate_migration_state(
                version_table_exists=version_table_exists,
                version_columns=version_columns,
                version_primary_key=version_primary_key,
                recorded_versions=recorded_versions,
                existing_managed_tables=existing_managed,
                has_rollout_cutover_column=ROLLOUT_CUTOFF_COLUMN in rollout_columns,
                has_site_activation_column=SITE_ACTIVATION_COLUMN in site_columns,
                negative_opening_check_state=(
                    "legacy" if not version_table_exists or legacy_checks == expected_legacy_checks
                    else "relaxed" if not legacy_checks else "partial"
                ),
            )
            if version_table_exists and recorded_versions:
                from migrations.site_inventory_rollout_cutover import _validate_existing_column

                if len(recorded_versions) >= 2 and not _validate_existing_column(connection, schema):
                    raise MigrationPreflightError("Recorded rollout cutover column has an unexpected definition")
                if len(recorded_versions) >= 4:
                    from migrations.site_inventory_site_activation import _validate_column

                    if not _validate_column(connection, schema):
                        raise MigrationPreflightError("Recorded Site activation column has an unexpected definition")
                if recorded_versions:
                    from models import (
                        SiteAlias,
                        SiteBalance,
                        SiteMaterialMovement,
                        SiteOpeningBalanceRequest,
                        SiteTransfer,
                        SiteTransferCorrection,
                        SiteTransferDispatch,
                        SiteTransferItem,
                        SiteTransferReceipt,
                        SiteTransferReceiptItem,
                    )

                    expected_tables = []
                    if len(recorded_versions) >= 1:
                        expected_tables.extend((
                            SiteAlias.__table__, SiteBalance.__table__, SiteMaterialMovement.__table__,
                            SiteOpeningBalanceRequest.__table__,
                        ))
                    if len(recorded_versions) >= 3:
                        expected_tables.extend((
                            SiteTransfer.__table__, SiteTransferItem.__table__, SiteTransferDispatch.__table__,
                            SiteTransferReceipt.__table__, SiteTransferReceiptItem.__table__,
                            SiteTransferCorrection.__table__,
                        ))
                    for table in expected_tables:
                        validate_model_table(connection, schema, table)
            transaction.commit()
            return {
                "database": database_name,
                "schema": active_schema,
                "server_version_num": str(server_version_num),
                "state": state,
                "versions": recorded_versions,
                "version_security": security,
            }
        except Exception:
            transaction.rollback()
            raise


def execute_migrations(engine, schema: str, *, execute: bool, migration_specs=None) -> list[dict]:
    specs = MIGRATION_SPECS if migration_specs is None else migration_specs
    if not execute:
        return []
    results = []
    for expected_version, migrate in specs:
        result = migrate(engine, schema=schema)
        if result.get("version") != expected_version:
            raise RuntimeError("Migration returned an unexpected version; runner stopped")
        results.append(result)
    return results


def _create_engine(database_url: str):
    url = make_url(database_url)
    if url.get_backend_name() == "postgresql":
        url = url.set(drivername="postgresql+psycopg")
    return create_engine(
        url,
        pool_size=2,
        max_overflow=0,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={
            "connect_timeout": 8,
            "options": "-c lock_timeout=5000 -c statement_timeout=30000",
        },
    )


def _print_preflight(identity: dict, report: dict, *, execute: bool, url_env_name: str) -> None:
    security = report["version_security"]
    if not security["exists"]:
        protection = "will-be-hardened-by-foundation"
    else:
        protection = "PASS" if security["protected"] else "will-be-hardened-by-foundation"
    print("SITE INVENTORY PRODUCTION MIGRATION PREFLIGHT")
    print("APP_ENV=production")
    print(f"database_url_env={url_env_name}")
    print(f"host={identity['host']}")
    print(f"port={identity['port']}")
    print(f"database={report['database']}")
    print(f"schema={report['schema']}")
    print(f"connection_mode={identity['connection_mode']}")
    print(f"production_project_ref_match={'YES' if identity['project_ref_match'] else 'NO'}")
    print(f"warehouse_test_ref_detected={'YES' if identity['test_project_ref_detected'] else 'NO'}")
    print("schema_prerequisites=PASS")
    print(f"migration_state={report['state']}")
    print(f"version_table_protection={protection}")
    print("migration_order=" + " -> ".join(MIGRATION_ORDER))
    print(f"mode={'EXECUTE' if execute else 'DRY_RUN'}")


def main(argv: list[str] | None = None, environ: dict[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url-env", required=True, help="Name of the explicit Production URL environment variable")
    parser.add_argument("--schema", required=True, help="Explicit Production schema; expected value is public")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Perform read-only preflight only (the default)")
    mode.add_argument("--execute", action="store_true", help="Apply the versioned migrations in their fixed order")
    args = parser.parse_args(argv)
    env = os.environ if environ is None else environ

    try:
        if str(env.get("APP_ENV", "")).strip() != "production":
            raise MigrationPreflightError("APP_ENV must equal production")
        if args.schema != "public":
            raise MigrationPreflightError("Production target schema must be explicitly set to public")
        database_url = resolve_explicit_database_url(args.database_url_env, env)
        identity = production_url_identity(database_url)
        # Pin model imports to the same explicitly selected target; never let them
        # initialize an engine from a different configured URL during preflight.
        os.environ["FTTH_DATABASE_URL"] = database_url
        engine = _create_engine(database_url)
        try:
            report = preflight_database(engine, args.schema)
            _print_preflight(identity, report, execute=args.execute, url_env_name=args.database_url_env)
            if not args.execute:
                print("result=PASS; no changes made")
                return 0

            with engine.connect() as lock_connection:
                locked = lock_connection.execute(
                    text("SELECT pg_try_advisory_lock(:key)"), {"key": _ADVISORY_LOCK_KEY}
                ).scalar_one()
                if not locked:
                    raise MigrationPreflightError("Another Site Inventory migration run is active; no changes made")
                lock_connection.commit()
                try:
                    preflight_database(engine, args.schema)
                    results = execute_migrations(engine, args.schema, execute=True)
                    final_report = preflight_database(engine, args.schema)
                    if final_report["state"] != "complete":
                        raise RuntimeError("Post-migration verification did not find all migration versions")
                    if not final_report["version_security"].get("protected"):
                        raise RuntimeError("Migration version table protection verification failed")
                    for result in results:
                        print(f"migration={result['version']} status={'already-applied' if result.get('already_applied') else 'applied'}")
                    print("post_migration_state=complete")
                    print("version_table_protection=PASS")
                finally:
                    lock_connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _ADVISORY_LOCK_KEY})
                    lock_connection.commit()
        finally:
            engine.dispose()
    except MigrationPreflightError as exc:
        print(f"ABORT: {exc}", file=sys.stderr)
        return 2
    except Exception:
        print("ABORT: connection, migration, or postflight failed; execution stopped at the first failure. No credentials were logged.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
