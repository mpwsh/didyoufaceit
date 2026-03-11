"""Basic smoke tests."""

import pytest

from didyoufaceit.cache import close_db, init_db


@pytest.fixture(autouse=True)
async def _cleanup_pool():
    """Reset the connection pool between tests."""
    yield
    await close_db()


async def test_init_db():
    """Verify DB schema can be created."""
    await init_db()


async def test_init_db_idempotent():
    """Verify init_db can be called twice without error."""
    await init_db()
    await init_db()
