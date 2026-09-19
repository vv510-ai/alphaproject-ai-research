"""Read-only configuration status. Never returns credentials or claims connectivity."""
import os
from fastapi import APIRouter

from app.config import Settings


def build_runtime_router(config: Settings) -> APIRouter:
    router = APIRouter(prefix="/runtime", tags=["local runtime"])

    @router.get("/status")
    def status() -> dict:
        primary = bool(config.LLM_PRIMARY_API_KEY and config.LLM_PRIMARY_BASE_URL
                       and config.LLM_PRIMARY_MODEL)
        backup = bool(config.LLM_BACKUP_API_KEY and config.LLM_BACKUP_BASE_URL
                      and config.LLM_BACKUP_MODEL)
        return {
            "app_id": "alphaproject-local",
            "process_id": os.getpid(),
            "instance_id": os.environ.get("ALPHA_INSTANCE_ID", "manual"),
            "mode": "local-monitoring",
            "llm_mode": "template" if config.llm_template_mode else "configured",
            "primary": {"configured": primary, "model": config.LLM_PRIMARY_MODEL if primary else None},
            "backup": {"configured": backup, "model": config.LLM_BACKUP_MODEL if backup else None},
            "lark_configured": bool(config.LARK_APP_ID and config.LARK_APP_SECRET and config.LARK_RECEIVE_ID),
            "market_data": "腾讯行情 / AKShare（联网获取）",
            "live_order_execution": False,
        }

    return router
