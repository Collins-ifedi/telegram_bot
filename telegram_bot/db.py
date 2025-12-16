# db.py
# Production-grade async database layer (PostgreSQL + asyncpg)

import logging
from typing import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    create_async_engine,
    AsyncSession,
    async_sessionmaker,
)
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import QueuePool

from config import settings
from models import Base

# Configure logging
logger = logging.getLogger(__name__)

# ---------- ASYNC ENGINE ----------

# Ensure the URL is set for asyncpg. 
# If settings.DATABASE_URL is "postgresql://...", replace it with "postgresql+asyncpg://..."
# This is a common safety check for async migrations.
database_url = settings.DATABASE_URL
if database_url and database_url.startswith("postgresql://"):
    database_url = database_url.replace("postgresql://", "postgresql+asyncpg://", 1)

engine = create_async_engine(
    database_url,
    echo=False,  # Set to True only for local debugging
    pool_pre_ping=True,  # Vital for recovering from dropped connections
)

# Async session factory
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,  # Important for async (avoids implicit IO on attribute access)
)

# ---------- AUTO MIGRATION / INIT ----------

async def init_db() -> None:
    """
    Asynchronously creates all tables if they do not exist.
    Acts as zero-touch migration for schema additions.
    """
    try:
        async with engine.begin() as conn:
            # run_sync bridges the async connection to the sync metadata create_all
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database schema initialized successfully (Async).")
    except SQLAlchemyError as e:
        logger.error(f"Database initialization failed: {e}")
        raise RuntimeError(f"Database initialization failed: {e}") from e

# ---------- SESSION HANDLING ----------

async def get_db_session() -> AsyncSession:
    """
    Returns a fresh async DB session.
    Useful for manual session management if needed.
    """
    return AsyncSessionLocal()


@asynccontextmanager
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    Provides a safe, transactional async DB session context manager.
    Handles commit on success, rollback on exception, and ensures close().
    Usage:
        async with get_db() as db:
            result = await db.execute(...)
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception as e:
            await session.rollback()
            logger.error(f"DB transaction rolled back due to error: {e}")
            raise
        finally:
            await session.close()

# ---------- HEALTH CHECK ----------

async def db_healthcheck() -> bool:
    """
    Verifies DB connectivity by executing a lightweight async query.
    """
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except SQLAlchemyError as e:
        logger.error(f"DB health check failed: {e}")
        return False

# ---------- TEST SCRIPT ----------

if __name__ == "__main__":
    import asyncio
    
    logging.basicConfig(level=logging.INFO)

    async def main():
        try:
            print("Initializing database (Async)...")
            await init_db()
            print("DB initialized ✔")

            print("Running health check...")
            if await db_healthcheck():
                print("DB connection OK ✔")
            else:
                print("DB connection FAILED ✖")
                exit(1)
        except RuntimeError as e:
            print(f"FATAL ERROR during DB setup: {e}")
            exit(1)

    asyncio.run(main())