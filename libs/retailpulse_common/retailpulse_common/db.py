"""SQLAlchemy engine, session factory and the declarative base.

Design notes worth defending in an interview:

* Each service owns its own database and never reaches into another service's
  tables. Cross-service reads go over HTTP or arrive as Kafka events.
* Sessions are request-scoped via a FastAPI dependency. A request either
  commits its unit of work or rolls the whole thing back -- there is no
  partially-applied request.
* ``pool_pre_ping`` is on because Postgres connections die silently when a
  container restarts; without it the first request after a restart fails.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    """Declarative base shared by all services' models."""


def build_engine(database_url: str, **kwargs: Any) -> Engine:
    """Create an engine, tuned differently for SQLite (tests) vs Postgres."""
    if database_url.startswith("sqlite"):
        # SQLite is used only by fast unit tests. StaticPool + check_same_thread
        # keep an in-memory database alive across the TestClient's threads.
        from sqlalchemy.pool import StaticPool

        return create_engine(
            database_url,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
            future=True,
        )

    return create_engine(
        database_url,
        pool_size=kwargs.pop("pool_size", 5),
        max_overflow=kwargs.pop("max_overflow", 10),
        pool_pre_ping=kwargs.pop("pool_pre_ping", True),
        future=True,
        **kwargs,
    )


class Database:
    """Owns one engine + session factory for a single service."""

    def __init__(self, database_url: str, **engine_kwargs: Any) -> None:
        self.engine = build_engine(database_url, **engine_kwargs)
        self.session_factory = sessionmaker(
            bind=self.engine, autoflush=False, autocommit=False, expire_on_commit=False
        )

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Transactional scope: commit on success, roll back on any exception."""
        session = self.session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def get_session(self) -> Iterator[Session]:
        """FastAPI dependency form of :meth:`session`."""
        with self.session() as session:
            yield session

    def ping(self) -> bool:
        """Cheap readiness check -- is the database actually reachable?"""
        try:
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    def create_all(self) -> None:
        """Create tables directly.

        Only used by tests. Real environments migrate with Alembic so that
        schema changes are versioned and reviewable.
        """
        Base.metadata.create_all(self.engine)
