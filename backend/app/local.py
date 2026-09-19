"""Serve the built dashboard and API together on the local loopback address."""
from pathlib import Path

from fastapi.staticfiles import StaticFiles

from app.api.runtime import build_runtime_router
from app.config import settings
from app.main import app

app.include_router(build_runtime_router(settings))
_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if not (_dist / "index.html").is_file():
    raise RuntimeError("Frontend build missing. Run npm run build in AlphaProject/frontend first.")
app.mount("/", StaticFiles(directory=_dist, html=True), name="dashboard")
