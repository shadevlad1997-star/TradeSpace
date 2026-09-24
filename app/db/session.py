from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from app.core.config import settings
engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True, future=True)
AsyncSessionLocal = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
async def get_db():
    async with AsyncSessionLocal() as session:
        from fastapi import HTTPException
        from app.services.integration_modes import verify_environment, IntegrationModeError
        try:
            await verify_environment(session)
        except IntegrationModeError as exc:
            raise HTTPException(exc.status, {'code': exc.code, 'message': str(exc)}) from exc
        yield session
