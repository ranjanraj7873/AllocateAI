"""
Database wiring.

Uses SQLite by default so the project runs with zero external setup.
Swapping to Postgres for a real deployment is a one-line change: set the
DATABASE_URL environment variable to e.g.
    postgresql+psycopg2://user:pass@host:5432/allocation
SQLAlchemy's engine abstraction means nothing else in the codebase needs
to change.
"""
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "sqlite:///./internship_allocation.db"
)

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """FastAPI dependency that yields a request-scoped DB session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
