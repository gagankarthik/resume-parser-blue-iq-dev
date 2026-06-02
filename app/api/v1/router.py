from fastapi import APIRouter
from app.api.v1.endpoints import resume, batch, webhooks, health

router = APIRouter(prefix="/api/v1")
router.include_router(health.router)
router.include_router(resume.router)
router.include_router(batch.router)
router.include_router(webhooks.router)
