"""List Feishu chats and optionally save one as the AlphaProject receive target.

The App Secret is read from AlphaProject/.env by the existing Settings object and
is never printed. This helper does not send a test message.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import lark_oapi as lark


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from app.config import settings  # noqa: E402


def list_chats() -> list[tuple[str, str]]:
    client = (
        lark.Client.builder()
        .app_id(settings.LARK_APP_ID)
        .app_secret(settings.LARK_APP_SECRET)
        .log_level(lark.LogLevel.ERROR)
        .build()
    )

    chats: list[tuple[str, str]] = []
    page_token: str | None = None
    while True:
        builder = (
            lark.im.v1.ListChatRequest.builder()
            .page_size(100)
            .user_id_type("open_id")
        )
        if page_token:
            builder.page_token(page_token)
        response = client.im.v1.chat.list(builder.build())
        if not response.success():
            raise RuntimeError(
                f"飞书列出群聊失败：code={response.code}, msg={response.msg}, "
                f"log_id={response.get_log_id()}"
            )
        data = response.data
        for item in data.items or []:
            chats.append((item.name or "未命名群聊", item.chat_id or ""))
        if not data.has_more:
            break
        page_token = data.page_token
        if not page_token:
            break
    return chats


def save_receive_id(chat_id: str) -> None:
    env_path = PROJECT_ROOT / ".env"
    text = env_path.read_text(encoding="utf-8")
    updated, count = re.subn(
        r"(?m)^LARK_RECEIVE_ID=.*$",
        f"LARK_RECEIVE_ID={chat_id}",
        text,
    )
    if count != 1:
        raise RuntimeError(".env 中应有且仅有一行 LARK_RECEIVE_ID=，请先检查配置文件")
    env_path.write_text(updated, encoding="utf-8")


def main() -> int:
    if not settings.LARK_APP_ID:
        print("未配置 LARK_APP_ID，请检查 AlphaProject/.env")
        return 2
    if not settings.LARK_APP_SECRET:
        print("未配置 LARK_APP_SECRET。请从飞书凭证页复制到本机 .env；不要粘贴到聊天中。")
        return 2

    try:
        chats = list_chats()
    except Exception as exc:  # noqa: BLE001 - keep the helper actionable for end users
        print(str(exc))
        print("如果返回权限不足，请在飞书权限管理中补充群聊读取权限后重试。")
        return 1

    if not chats:
        print("没有列出群聊。请先把机器人加入目标群，再重试。")
        return 1

    print("可用群聊（只显示名称和 chat_id，不显示任何凭证）：")
    for index, (name, chat_id) in enumerate(chats):
        print(f"[{index}] {name}\t{chat_id}")

    choice = input("输入目标群编号（直接回车取消）：").strip()
    if not choice:
        print("已取消，未修改 .env")
        return 0
    try:
        index = int(choice)
        name, chat_id = chats[index]
    except (ValueError, IndexError):
        print("群编号无效，未修改 .env")
        return 2
    if not chat_id:
        print("目标群没有有效 chat_id，未修改 .env")
        return 2

    save_receive_id(chat_id)
    print(f"已将 LARK_RECEIVE_ID 设置为群聊“{name}”的 chat_id。")
    print("下一步：重启 AlphaProject 后端，再调用 /push/status 检查 lark_configured。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
