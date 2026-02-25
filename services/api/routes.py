"""Combines sub-routers into the top-level API router."""

from fastapi import APIRouter

from routes_health import router as health_router
from routes_system import router as system_router
from routes_data import router as data_router
from routes_system_info import router as system_info_router
from routes_webhooks import router as webhooks_router

router = APIRouter()

router.include_router(health_router)
router.include_router(system_router)
router.include_router(data_router)
router.include_router(system_info_router)
router.include_router(webhooks_router)
