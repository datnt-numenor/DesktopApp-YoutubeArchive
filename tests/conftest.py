from collections.abc import AsyncGenerator
from pathlib import Path
import sys

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database import Base  # noqa: E402
import models  # noqa: E402,F401


@pytest_asyncio.fixture
async def session(tmp_path: Path) -> AsyncGenerator[AsyncSession, None]:
    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}"
    engine = create_async_engine(db_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_factory() as db_session:
        yield db_session

    await engine.dispose()
