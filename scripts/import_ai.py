"""Import only the selected official model route; budgets must be explicitly supplied."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.app.ai_config import AIConfig, ConfigFailure, read_source_fields, save_config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--daily-budget', required=True)
    parser.add_argument('--monthly-budget', required=True)
    args = parser.parse_args()
    base, model, key = read_source_fields(ROOT.parent / 'AlphaProject/.env')
    config = AIConfig(key, base, model, args.daily_budget, args.monthly_budget,
                      datetime.now(timezone.utc).isoformat())
    save_config(ROOT / '.runtime/ai-config.dpapi', config)
    print(json.dumps({'imported': True, 'model': model, 'daily_budget': config.daily_budget,
        'monthly_budget': config.monthly_budget, 'encrypted_for_current_windows_user': True,
        'model_called': False}, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except (ConfigFailure, OSError, ValueError, TypeError):
        print('AI 配置导入未完成，请检查来源、预算和当前 Windows 用户；未输出秘密值。', file=sys.stderr)
        raise SystemExit(1)
