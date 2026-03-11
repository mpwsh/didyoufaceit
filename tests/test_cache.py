"""Basic smoke tests."""

from didyoufaceit.cache import init_db


async def test_init_db():
    """Verify DB schema can be created."""
    await init_db()


async def test_init_db_idempotent():
    """Verify init_db can be called twice without error."""
    await init_db()
    await init_db()
