"""第一课：使用临时数据库，在两个独立进程中验证自选股保存。

从任意目录用项目 backend/.venv 的 Python 运行本文件。
不导入生产 app.main，不启动行情、模型、推送或定时任务。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from datetime import datetime, timezone


PROJECT = Path(__file__).resolve().parents[2]
CHILD = r'''
import json
import os
import sys
from pathlib import Path
from app.db import init_db
from app.errors import WatchlistError
from app.repositories.watchlist_repo import WatchlistRepo
from app.services.watchlist_service import WatchlistService

db_path, action = Path(sys.argv[1]), sys.argv[2]
if action == "write":
    init_db(db_path)
service = WatchlistService(repo=WatchlistRepo(db_path), max_total=30, max_holding=5)
if action == "write":
    service.add(code="000001", name="学习样例", is_holding=True)
    try:
        service.add(code="000001", name="学习样例")
    except WatchlistError:
        duplicate_rejected = True
    else:
        raise AssertionError("重复添加应被拒绝")
rows = service.snapshot()
assert len(rows) == 1, len(rows)
row = rows[0]
assert row["code"] == "000001"
assert row["name"] == "学习样例"
assert row["is_holding"] is True
result = {"action": action, "pid": os.getpid(), "rows": rows}
if action == "write":
    result["duplicate_rejected"] = duplicate_rejected
print(json.dumps(result, ensure_ascii=False))
'''


def main() -> None:
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    observations = []
    temp_root = (PROJECT / "backend" / "data" / "learning-temp").resolve()
    assert temp_root.is_relative_to(PROJECT)
    temp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="lesson-", dir=temp_root) as temporary:
        db_path = Path(temporary) / "lesson.db"
        assert db_path.resolve().is_relative_to(temp_root)
        for action in ("write", "read_after_restart"):
            completed = subprocess.run(
                [sys.executable, "-X", "utf8", "-B", "-c", CHILD, str(db_path), action],
                cwd=PROJECT / "backend", env=env, capture_output=True,
                text=True, encoding="utf-8", check=True, timeout=45,
            )
            observations.append(json.loads(completed.stdout))
        assert observations[0]["pid"] != observations[1]["pid"]
        assert observations[0]["rows"] == observations[1]["rows"]

    files = [
        "backend/app/api/watchlist.py", "backend/app/config.py",
        "backend/app/db.py", "backend/app/errors.py",
        "backend/app/models/watchlist.py",
        "backend/app/repositories/watchlist_repo.py",
        "backend/app/services/watchlist_service.py",
        "backend/app/services/stock_basic_service.py",
        "backend/app/events/watchlist_events.py",
        "backend/pyproject.toml", "backend/uv.lock",
        "specs/learning/watchlist_persistence_demo.py",
    ]
    snapshot = {
        name: hashlib.sha256((PROJECT / name).read_bytes()).hexdigest()
        for name in files
    }
    print(json.dumps({
        "checked_utc": datetime.now(timezone.utc).isoformat(),
        "status": "passed", "python": sys.version.split()[0],
        "scope": "local Watchlist service + repository + SQLite, two independent processes",
        "observations": observations, "source_sha256": snapshot,
        "temporary_database_removed": True,
        "not_verified": ["HTTP/API transport", "browser interaction", "production", "market data",
                         "LLM", "Feishu", "scheduler"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
