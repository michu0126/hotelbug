from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings

engine = create_async_engine(get_settings().database_url.get_secret_value(), pool_pre_ping=True)
Session = async_sessionmaker(engine, expire_on_commit=False)


async def get_session():
    async with Session() as session:
        yield session
