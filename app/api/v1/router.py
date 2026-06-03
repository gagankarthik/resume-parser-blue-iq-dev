from fastapi import APIRouter

from app.api.v1.endpoints import admin, batch, health, resume, webhooks

router = APIRouter(prefix="/api/v1")
router.include_router(health.router)
router.include_router(resume.router)
router.include_router(batch.router)
router.include_router(webhooks.router)
router.include_router(admin.router)
