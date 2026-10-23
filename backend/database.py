import os
from dotenv import load_dotenv
from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

load_dotenv()

DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts.db")
DATABASE_URL = f"sqlite:///{DB_FILE}"

# Standard SQLite engine with multi-thread check disabled for FastAPI
sync_engine = create_engine(
    DATABASE_URL,
    echo=False,
    connect_args={"check_same_thread": False, "timeout": 30}
)

# Enable WAL (Write-Ahead Logging) mode to handle 20+ concurrent users seamlessly
@event.listens_for(sync_engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA synchronous=NORMAL;")
    cursor.close()

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
