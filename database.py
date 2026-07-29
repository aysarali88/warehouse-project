import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

load_dotenv()

DEFAULT_PROGRAM = "FTTH"
SINGLE_RAN_PROGRAM = "SINGLE_RAN"


def normalize_program(value: str = "") -> str:
    text = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
    return SINGLE_RAN_PROGRAM if text in {"SINGLE_RAN", "SR", "SINGLERAN"} else DEFAULT_PROGRAM


def normalize_database_url(value: str) -> str:
    value = str(value or "").strip()
    if value and "://" not in value and "@" in value:
        value = f"postgresql://{value}"
    if value.startswith("postgresql://"):
        return value.replace("postgresql://", "postgresql+psycopg://", 1)
    return value


DATABASE_URL = normalize_database_url(os.getenv("FTTH_DATABASE_URL") or os.getenv("DATABASE_URL", "sqlite:///./rollout.db"))
SR_DATABASE_URL = os.getenv("SR_DATABASE_URL", "").strip()
if SR_DATABASE_URL:
    SR_DATABASE_URL = normalize_database_url(SR_DATABASE_URL)

DB_POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "5"))
DB_MAX_OVERFLOW = int(os.getenv("DB_MAX_OVERFLOW", "5"))
DB_POOL_TIMEOUT = int(os.getenv("DB_POOL_TIMEOUT", "30"))


def create_database_engine(url: str):
    is_sqlite = url.startswith("sqlite")
    connect_args = (
        {"check_same_thread": False}
        if is_sqlite
        else {
            "prepare_threshold": None,
            "connect_timeout": 8,
            "options": "-c lock_timeout=5000 -c statement_timeout=10000",
        }
    )
    pool_options = (
        {}
        if is_sqlite
        else {
            "pool_size": DB_POOL_SIZE,
            "max_overflow": DB_MAX_OVERFLOW,
            "pool_timeout": DB_POOL_TIMEOUT,
            "pool_recycle": 300,
        }
    )
    return create_engine(
        url,
        connect_args=connect_args,
        pool_pre_ping=True,
        **pool_options,
    )


engine = create_database_engine(DATABASE_URL)
sr_engine = create_database_engine(SR_DATABASE_URL) if SR_DATABASE_URL else None

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
SRSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=sr_engine) if sr_engine else None
Base = declarative_base()


def get_sessionmaker(program: str = DEFAULT_PROGRAM):
    if normalize_program(program) == SINGLE_RAN_PROGRAM and SRSessionLocal is not None:
        return SRSessionLocal
    return SessionLocal


def all_engines():
    engines = [(DEFAULT_PROGRAM, engine)]
    if sr_engine is not None and sr_engine.url != engine.url:
        engines.append((SINGLE_RAN_PROGRAM, sr_engine))
    return engines


def all_sessionmakers():
    sessions = [(DEFAULT_PROGRAM, SessionLocal)]
    if SRSessionLocal is not None and sr_engine.url != engine.url:
        sessions.append((SINGLE_RAN_PROGRAM, SRSessionLocal))
    return sessions
