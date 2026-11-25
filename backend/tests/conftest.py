import os
import tempfile

# Import order matters here: pytest imports this conftest.py before any
# test module, and app.database creates its SQLAlchemy engine at import
# time from whatever DATABASE_URL is in os.environ *right then* (module
# imports are cached, so setting the env var later has no effect on an
# already-created engine). Without this line, the very first import of
# app.database anywhere in the test session would silently bind to the
# real default `./internship_allocation.db` — the same file the actual
# app uses — and every test run would pollute (and keep re-polluting) the
# live demo database with fake test users. Setting an isolated temp path
# here, before anything imports app.database, keeps every test run fully
# disposable regardless of which test module happens to import it first.
os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.mkdtemp()}/conftest_isolated.db")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
