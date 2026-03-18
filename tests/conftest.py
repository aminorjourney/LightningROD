"""Root conftest: test engine, DB session with transaction rollback, Alembic migrations.

CRITICAL: Environment variables are set BEFORE any app imports to prevent
the production engine (db/engine.py) from connecting to the dev database.
"""

import os

# Set test environment BEFORE any app imports (Pitfall 3 from RESEARCH.md)
os.environ["POSTGRES_USER"] = "lightningrod_test"
os.environ["POSTGRES_PASSWORD"] = "testpass"
os.environ["POSTGRES_DB"] = "lightningrod_test"
os.environ["POSTGRES_HOST"] = "localhost"

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

TEST_DB_URL = "postgresql+asyncpg://lightningrod_test:testpass@localhost:5433/lightningrod_test"
TEST_DB_URL_SYNC = "postgresql://lightningrod_test:testpass@localhost:5433/lightningrod_test"


@pytest_asyncio.fixture(scope="session")
async def test_engine():
    """Create a test engine once per session and run Alembic migrations."""
    engine = create_async_engine(TEST_DB_URL, echo=False)

    # Run Alembic migrations using the sync driver (Alembic does not support async)
    from alembic.config import Config
    from alembic import command

    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", TEST_DB_URL_SYNC)
    alembic_cfg.set_main_option("script_location", "db/migrations")
    command.upgrade(alembic_cfg, "head")

    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(test_engine):
    """Per-test DB session with transaction rollback isolation.

    Each test gets a clean session backed by a transaction that rolls back
    after the test completes, so no data persists between tests.
    """
    async with test_engine.connect() as conn:
        trans = await conn.begin()
        session = AsyncSession(bind=conn, expire_on_commit=False)

        # Begin a nested savepoint so factories can flush() without ending
        # the outer transaction
        nested = await conn.begin_nested()

        yield session

        await session.close()
        if nested.is_active:
            await nested.rollback()
        await trans.rollback()


@pytest.fixture(autouse=True)
def reset_factories():
    """Reset factory seed before each test for deterministic data generation."""
    from tests.factories import BaseFactory

    BaseFactory.reset_seed()
    yield
