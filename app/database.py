from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy import inspect, text

from app.models import Base


class Database:
    def __init__(self, database_url: str) -> None:
        self.engine: AsyncEngine = create_async_engine(database_url, future=True)
        self.session_factory = async_sessionmaker(
            self.engine,
            expire_on_commit=False,
            class_=AsyncSession,
        )

    async def create_tables(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await self._ensure_added_columns(conn)

    async def _ensure_added_columns(self, conn) -> None:
        existing_columns = await conn.run_sync(
            lambda sync_conn: {
                table_name: {column["name"] for column in inspect(sync_conn).get_columns(table_name)}
                for table_name in inspect(sync_conn).get_table_names()
            }
        )
        migrations = {
            "translation_cache": [
                ("provider", "ALTER TABLE translation_cache ADD COLUMN provider VARCHAR(32) NOT NULL DEFAULT 'mock'"),
                ("model", "ALTER TABLE translation_cache ADD COLUMN model VARCHAR(128) NOT NULL DEFAULT 'mock'"),
            ],
            "guild_usage_monthly": [
                ("model", "ALTER TABLE guild_usage_monthly ADD COLUMN model VARCHAR(128) NOT NULL DEFAULT 'mock'"),
                ("input_tokens_used", "ALTER TABLE guild_usage_monthly ADD COLUMN input_tokens_used INTEGER NOT NULL DEFAULT 0"),
                ("output_tokens_used", "ALTER TABLE guild_usage_monthly ADD COLUMN output_tokens_used INTEGER NOT NULL DEFAULT 0"),
            ],
        }

        for table_name, table_migrations in migrations.items():
            table_columns = existing_columns.get(table_name, set())
            for column_name, ddl in table_migrations:
                if column_name not in table_columns:
                    await conn.execute(text(ddl))

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.session_factory() as session:
            yield session

    async def healthcheck(self) -> bool:
        try:
            async with self.session() as session:
                await session.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    async def close(self) -> None:
        await self.engine.dispose()
