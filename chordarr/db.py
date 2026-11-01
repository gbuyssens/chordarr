"""Database engine and session helpers (SQLite through SQLAlchemy async)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from chordarr.config import settings

engine = create_async_engine(f"sqlite+aiosqlite:///{settings.db_path}", echo=False)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


@event.listens_for(engine.sync_engine, "connect")
def _sqlite_pragmas(dbapi_connection, _record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=15000")
    cursor.close()


async def init_db() -> None:
    from chordarr import models  # noqa: F401  (register tables)
    from chordarr.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _migrate(conn)


async def _migrate(conn) -> None:
    """Tiny forward-only migrations: add columns that create_all cannot add to existing tables."""
    wanted = {
        "artists": {
            "deezer_id": "INTEGER",
            "image_url": "VARCHAR",
            "monitor_appearances": "BOOLEAN DEFAULT 1",
            "monitor_new_items": "VARCHAR DEFAULT 'all'",
        },
        "releases": {"cover_url": "VARCHAR", "last_search_at": "DATETIME"},
    }
    for table, columns in wanted.items():
        rows = await conn.execute(text(f"PRAGMA table_info({table})"))
        existing = {r[1] for r in rows}
        for name, ddl in columns.items():
            if name not in existing:
                await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency."""
    async with SessionLocal() as session:
        yield session
