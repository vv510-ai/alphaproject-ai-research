"""Start/stop only this Lab's local server. No cloud or production operations."""

import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import webbrowser

import httpx
import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.app.network import lan_addresses

RUNTIME = ROOT / ".runtime"
STATE = RUNTIME / "server.json"
APP_MODULE = "backend.app.main:app"


def health(port):
    try:
        with httpx.Client(trust_env=False, timeout=1) as client:
            response = client.get(f"http://127.0.0.1:{port}/api/health")
            response.raise_for_status()
            result = response.json()
        return result if result.get("service") == "AlphaProject-Lab" else None
    except (httpx.HTTPError, ValueError):
        return None


def checked_process(pid, created_at=None):
    try:
        process = psutil.Process(int(pid))
        if Path(process.cwd()).resolve() != ROOT or APP_MODULE not in process.cmdline():
            raise RuntimeError("进程身份与本工程不符，未执行停止。")
        if created_at is not None and abs(process.create_time() - created_at) > 0.01:
            raise RuntimeError("进程标识已经变化，未执行停止。")
        return process
    except psutil.NoSuchProcess:
        return None


def build():
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if not npm:
        raise RuntimeError("没有找到 npm，请先安装 Node 并运行准备开发环境入口。")
    if not (ROOT / "frontend" / "node_modules").is_dir():
        raise RuntimeError("前端依赖尚未安装，请先运行准备开发环境入口。")
    subprocess.run([npm, "run", "build"], cwd=ROOT / "frontend", check=True)


def describe(port):
    urls = [f"http://{ip}:{port}" for ip in lan_addresses()]
    print(f"电脑入口：http://127.0.0.1:{port}")
    for url in urls:
        print(f"同一 Wi-Fi 的手机入口：{url}")
    print("数据库：.runtime/watchlist.sqlite3；停止或重启服务不会删除列表。")
    print("手机实际访问仍需核验；保持电脑运行并连接同一 Wi-Fi。")
    return urls[0] if urls else f"http://127.0.0.1:{port}"


def start(port, skip_build=False, open_browser=False):
    cloud_marker = RUNTIME/'cloud-primary.json'
    if cloud_marker.is_file():
        cloud = json.loads(cloud_marker.read_text(encoding='utf-8'))
        raise RuntimeError(f"学习版已经迁到云端：{cloud['url']}。本机启动已保护性停止，避免两处重复通知或分别计费；本地开发请使用隔离测试环境。")
    if not skip_build:
        build()
    RUNTIME.mkdir(exist_ok=True)
    current = health(port)
    if current:
        process = checked_process(current["pid"])
        if not process:
            raise RuntimeError("服务状态已经改变，请重试。")
        print("本工程的服务已经在运行。")
    else:
        # Check without taking over another program's port.
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                raise RuntimeError(f"端口 {port} 被其他程序占用，未停止或替换它。")
        with (RUNTIME / "server.log").open("ab") as log:
            child = subprocess.Popen(
                [sys.executable, "-X", "utf8", "-m", "uvicorn", APP_MODULE,
                 "--host", "0.0.0.0", "--port", str(port), "--workers", "1", "--no-proxy-headers"],
                cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                env={**os.environ, 'ALPHA_LAB_PORT': str(port)},
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            current = health(port)
            if current:
                break
            if child.poll() is not None:
                raise RuntimeError("服务未能启动，请查看 .runtime/server.log。")
            time.sleep(0.25)
        if not current:
            raise RuntimeError("尚未取得启动确认，请查看 .runtime/server.log 后重试。")
        process = checked_process(current["pid"])
        if not process:
            raise RuntimeError("服务启动后已退出，请查看日志。")
        print("共享自选服务已启动。")
    STATE.write_text(json.dumps({
        "pid": process.pid, "created_at": process.create_time(), "port": port,
        "instance_id": current["instance_id"], "root": str(ROOT),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    url = describe(port)
    if open_browser:
        webbrowser.open(url)


def stop():
    if not STATE.exists():
        print("没有本工程启动入口记录的进程，未停止其他程序。")
        return
    state = json.loads(STATE.read_text(encoding="utf-8"))
    process = checked_process(state["pid"], state["created_at"])
    if process:
        process.terminate()
        process.wait(timeout=8)
        print("共享自选服务已停止，数据库保留。")
    else:
        print("记录中的服务已经停止，数据库保留。")
    STATE.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="AlphaProject Lab local launcher")
    parser.add_argument("action", choices=["start", "stop", "restart", "status"])
    parser.add_argument("--port", type=int, default=18765)
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--open", action="store_true", dest="open_browser")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("端口必须在 1024 到 65535 之间")
    if args.action in ("stop", "restart"):
        stop()
    if args.action in ("start", "restart"):
        start(args.port, args.skip_build, args.open_browser)
    elif args.action == "status":
        print(json.dumps(health(args.port) or {"status": "not_running"}, ensure_ascii=False))
        describe(args.port)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError, psutil.Error) as error:
        print(f"操作未完成：{error}", file=sys.stderr)
        raise SystemExit(1)
