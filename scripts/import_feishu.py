"""Explicitly reuse only four authorized Feishu settings; never print secret values."""

from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.app.feishu_config import ConfigFailure, FeishuConfig, read_source_fields, save_config
from backend.app.feishu import FeishuClient, FeishuFailure


def main():
    values = read_source_fields(ROOT.parent / "AlphaProject" / ".env")
    now = datetime.now(timezone.utc).isoformat()
    pending = FeishuConfig(values["LARK_APP_ID"], values["LARK_APP_SECRET"], values["LARK_RECEIVE_ID"], "", "", "", now)
    target = FeishuClient(pending).verify_target(allow_unbound=True)
    config = FeishuConfig(pending.app_id, pending.app_secret, pending.chat_id,
                          target.owner_id, target.group_name, target.bot_name, now)
    save_config(ROOT / ".runtime" / "feishu-config.dpapi", config)
    print(json.dumps({"imported": True, "group_name": target.group_name, "bot_name": target.bot_name,
                      "human_members": 1, "bots": 1, "owner_matches_member": True,
                      "target_fingerprint": config.fingerprint, "at": now,
                      "encrypted_for_current_windows_user": True, "message_sent": False}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (ConfigFailure, FeishuFailure) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
    except (OSError, ValueError, TypeError):
        print("配置导入未完成；请检查文件与网络，不会输出配置内容。", file=sys.stderr)
        raise SystemExit(1)
