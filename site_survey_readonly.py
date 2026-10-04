"""Read-only access to installed pole counts from the Site Survey database."""

from functools import lru_cache
import os

from sqlalchemy import create_engine, text


def normalize_city(value):
    return "".join(char.lower() for char in str(value or "") if char.isalnum())


def canonical_city(value):
    key = normalize_city(value)
    if key in {"misrata", "misurata"}:
        return "Misurata"
    if key == "tripoli":
        return "Tripoli"
    return ""


def empty_result():
    return {
        "available": False,
        "total": None,
        "by_city": {"Misurata": None, "Tripoli": None},
        "last_updated": None,
    }


@lru_cache(maxsize=2)
def _survey_engine(database_url):
    url = database_url.strip()
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://") :]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    return create_engine(
        url,
        pool_size=1,
        max_overflow=0,
        pool_timeout=3,
        pool_pre_ping=True,
        connect_args={
            "connect_timeout": 5,
            "options": "-c default_transaction_read_only=on -c statement_timeout=5000 -c lock_timeout=1000",
        },
    )


def installed_pole_counts():
    database_url = os.getenv("SITE_SURVEY_READONLY_DATABASE_URL", "").strip()
    if not database_url:
        return empty_result()

    try:
        with _survey_engine(database_url).connect() as connection:
            with connection.begin():
                connection.execute(text("SET TRANSACTION READ ONLY"))
                rows = connection.execute(
                    text(
                        """
                        SELECT city, COUNT(*) AS pole_count, MAX(updated_at) AS last_updated
                        FROM public.column_checks
                        WHERE is_planted IS TRUE
                        GROUP BY city
                        """
                    )
                ).mappings()
                counts = {"Misurata": 0, "Tripoli": 0}
                last_updated = None
                for row in rows:
                    city = canonical_city(row["city"])
                    if not city:
                        continue
                    counts[city] += int(row["pole_count"] or 0)
                    timestamp = row["last_updated"]
                    if timestamp and (last_updated is None or timestamp > last_updated):
                        last_updated = timestamp
        return {
            "available": True,
            "total": sum(counts.values()),
            "by_city": counts,
            "last_updated": last_updated.isoformat() if last_updated else None,
        }
    except Exception:
        return empty_result()
