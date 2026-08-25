import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
EXCEPTIONS_FILE = ROOT / "docs" / "rls_exceptions.json"

sys.path.insert(0, str(ROOT))

from database import normalize_database_url  # noqa: E402
from models import Base  # noqa: E402


def application_tables() -> set[str]:
    return {table.name for table in Base.metadata.sorted_tables}


def load_exceptions(path: Path = EXCEPTIONS_FILE) -> dict[str, str]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    exceptions = data.get("public_rls_disabled_exceptions", {})
    if not isinstance(exceptions, dict):
        raise ValueError("public_rls_disabled_exceptions must be an object")
    return {str(name): str(reason).strip() for name, reason in exceptions.items()}


def validate_exception_reasons(exceptions: dict[str, str], app_tables: set[str]) -> list[str]:
    errors = []
    for table, reason in sorted(exceptions.items()):
        if table not in app_tables:
            errors.append(f"Exception table is not application-owned: {table}")
        if not reason:
            errors.append(f"Exception must include a reason: {table}")
    return errors


def disabled_rls_tables(database_url: str) -> list[str]:
    engine = create_engine(database_url, pool_pre_ping=True)
    query = text(
        """
        SELECT tablename
        FROM pg_tables
        WHERE schemaname = 'public'
          AND rowsecurity = false
        ORDER BY tablename
        """
    )
    with engine.connect() as conn:
        return [row[0] for row in conn.execute(query)]


def public_table_rls_status(database_url: str) -> list[tuple[str, bool]]:
    engine = create_engine(database_url, pool_pre_ping=True)
    query = text(
        """
        SELECT tablename, rowsecurity
        FROM pg_tables
        WHERE schemaname = 'public'
        ORDER BY tablename
        """
    )
    with engine.connect() as conn:
        return [(row[0], bool(row[1])) for row in conn.execute(query)]


def main() -> int:
    parser = argparse.ArgumentParser(description="Fail if public application tables have RLS disabled.")
    parser.add_argument("--database-url", default="", help="PostgreSQL URL. Defaults to FTTH_DATABASE_URL or DATABASE_URL.")
    parser.add_argument("--list-app-tables", action="store_true", help="Print application-owned table names and exit.")
    parser.add_argument("--audit", action="store_true", help="Print public table RLS status before validating.")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    app_tables = application_tables()
    exceptions = load_exceptions()
    exception_errors = validate_exception_reasons(exceptions, app_tables)
    if exception_errors:
        print("Invalid RLS exception configuration:")
        for error in exception_errors:
            print(f"- {error}")
        return 1

    if args.list_app_tables:
        print("\n".join(sorted(app_tables)))
        return 0

    raw_url = args.database_url or os.getenv("FTTH_DATABASE_URL") or os.getenv("DATABASE_URL") or ""
    if not raw_url:
        print("No PostgreSQL database URL found. Set FTTH_DATABASE_URL or DATABASE_URL.")
        return 2

    database_url = normalize_database_url(raw_url)
    if not database_url.startswith("postgresql"):
        print("RLS validation requires PostgreSQL. SQLite/local file databases do not support RLS.")
        return 2

    if args.audit:
        print("Public schema RLS audit:")
        for table, enabled in public_table_rls_status(database_url):
            owner = "application" if table in app_tables else "non-application"
            status = "enabled" if enabled else "disabled"
            exempt = " exempt" if table in exceptions else ""
            print(f"- {table}: {status} ({owner}{exempt})")

    disabled = set(disabled_rls_tables(database_url))
    unexpected = sorted((disabled & app_tables) - set(exceptions))
    if unexpected:
        print("RLS is disabled on public application table(s):")
        for table in unexpected:
            print(f"- {table}")
        print("Enable RLS in the migration or document an explicit exception in docs/rls_exceptions.json.")
        return 1

    print(f"RLS validation passed for {len(app_tables)} public application tables.")
    if exceptions:
        print("Documented RLS-disabled exception(s):")
        for table, reason in sorted(exceptions.items()):
            print(f"- {table}: {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
