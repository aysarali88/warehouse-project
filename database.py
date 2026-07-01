import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./rollout.db")

if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

is_sqlite = DATABASE_URL.startswith("sqlite")
if "pooler.supabase.com:5432" in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("pooler.supabase.com:5432", "pooler.supabase.com:6543")

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
        "pool_size": 2,
        "max_overflow": 0,
        "pool_timeout": 10,
        "pool_recycle": 300,
    }
)

engine = create_engine(
    DATABASE_URL,
    connect_args=connect_args,
    pool_pre_ping=True,
    **pool_options,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()
