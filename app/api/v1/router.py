from fastapi import APIRouter
from app.api.v1 import auth, admin, merchant, cabinet, aggregator, integration
api_router=APIRouter(prefix='/api/v1')
api_router.include_router(auth.router)
api_router.include_router(admin.router)
api_router.include_router(merchant.router)
api_router.include_router(aggregator.router)
api_router.include_router(cabinet.router)
api_router.include_router(integration.router)
