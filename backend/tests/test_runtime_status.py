from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.runtime import build_runtime_router
from app.config import Settings


def test_status_does_not_expose_credentials_or_claim_verified_connection():
    config = Settings(_env_file=None, LLM_PRIMARY_API_KEY="test-secret",
                      LLM_PRIMARY_BASE_URL="https://example.test/v1",
                      LLM_PRIMARY_MODEL="test-model", LARK_APP_SECRET="lark-secret")
    app = FastAPI()
    app.include_router(build_runtime_router(config))
    response = TestClient(app).get("/runtime/status")
    assert response.status_code == 200
    assert "test-secret" not in response.text
    assert "lark-secret" not in response.text
    assert "https://example.test" not in response.text
    assert response.json()["primary"] == {"configured": True, "model": "test-model"}
    assert response.json()["lark_configured"] is False
    assert response.json()["live_order_execution"] is False


def test_unconfigured_runtime_reports_template_mode():
    app = FastAPI()
    app.include_router(build_runtime_router(Settings(_env_file=None)))
    status = TestClient(app).get("/runtime/status").json()
    assert status["llm_mode"] == "template"
    assert status["primary"] == {"configured": False, "model": None}
