import os
from dotenv import load_dotenv
from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

load_dotenv()

def database_file() -> str:
    """DATABASE_PATH puts the database somewhere else, for example on a persistent disk when the app is deployed (otherwise it is lost on every redeploy)."""
    path = os.path.abspath(os.getenv("DATABASE_PATH") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts.db"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL:
    DATABASE_URL = DATABASE_URL.strip()
if not DATABASE_URL:
    DB_FILE = database_file()
    DATABASE_URL = f"sqlite:///{DB_FILE}"

# SQLAlchemy 2.0 defaults postgresql:// to 'psycopg' (psycopg3). Explicitly use psycopg2 driver.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg2://", 1)
elif DATABASE_URL.startswith("postgresql://") and not DATABASE_URL.startswith("postgresql+"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)

# Ensure sslmode=require for Supabase / remote postgres if not already present
if not DATABASE_URL.startswith("sqlite") and "sslmode=" not in DATABASE_URL:
    sep = "&" if "?" in DATABASE_URL else "?"
    DATABASE_URL = f"{DATABASE_URL}{sep}sslmode=require"

is_sqlite = DATABASE_URL.startswith("sqlite")

# Engine initialization
if is_sqlite:
    sync_engine = create_engine(
        DATABASE_URL,
        echo=False,
        connect_args={"check_same_thread": False, "timeout": 30}
    )

    # Enable WAL mode for SQLite
    @event.listens_for(sync_engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.close()
else:
    # PostgreSQL / Supabase with connection pooling
    sync_engine = create_engine(
        DATABASE_URL,
        echo=False,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20
    )

SyncSessionLocal = sessionmaker(
    bind=sync_engine,
    autocommit=False,
    autoflush=False
)
SessionLocal = SyncSessionLocal
Base = declarative_base()

def get_db():
    """Database session dependency for FastAPI routes."""
    session = SyncSessionLocal()
    try:
        yield session
    finally:
        session.close()
