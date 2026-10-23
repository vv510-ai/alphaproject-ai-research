"""An explicit, encrypted configuration for this Lab's single model route."""
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re

from .feishu_config import ConfigFailure, _crypt, service_credential


@dataclass(frozen=True)
class AIConfig:
    api_key: str = field(repr=False)
    base_url: str
    model: str
    daily_budget: str
    monthly_budget: str
    imported_at: str

    def validate(self):
        if self.base_url != "https://api.deepseek.com/v1" or self.model != "deepseek-flash":
            raise ConfigFailure("学习版目前只支持已核验的 DeepSeek Flash 官方接口。")
        if not isinstance(self.api_key, str) or not 10 <= len(self.api_key) <= 512 or any(c.isspace() for c in self.api_key):
            raise ConfigFailure("AI 密钥格式无法确认，请重新导入。")
        try:
            budgets = [Decimal(value) for value in (self.daily_budget, self.monthly_budget)]
            if any(not value.is_finite() or value <= 0 or value > 1000 for value in budgets) or budgets[0] > budgets[1]:
                raise ValueError()
        except (ValueError, TypeError, InvalidOperation):
            raise ConfigFailure("AI 日/月预算须为正数，月预算不能小于日预算。") from None


def save_config(path: Path, config: AIConfig):
    config.validate()
    plain = json.dumps(asdict(config), ensure_ascii=False).encode()
    encrypted = _crypt(plain, purpose="AI")
    if _crypt(encrypted, True, purpose="AI") != plain:
        raise ConfigFailure("AI 配置加密回读失败，未保存。")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_bytes(encrypted)
    os.replace(temporary, path)


def load_config(path: Path) -> AIConfig | None:
    credential = service_credential('ai.json')
    if credential is None and not path.is_file():
        return None
    try:
        values = credential if credential is not None else json.loads(_crypt(path.read_bytes(), True, purpose="AI"))
        result = AIConfig(**values)
        result.validate()
        return result
    except (OSError, ValueError, TypeError, ConfigFailure):
        raise ConfigFailure("AI 配置无法读取，请核对当前运行环境的凭据。") from None


def read_source_fields(path: Path):
    allowed = {"LLM_PRIMARY_BASE_URL", "LLM_PRIMARY_API_KEY", "LLM_PRIMARY_MODEL"}
    values = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        match = re.match(r"^\s*([A-Z0-9_]+)\s*=\s*(.*?)\s*$", line)
        if not match or match[1] not in allowed:
            continue
        key, value = match.groups()
        if key in values:
            raise ConfigFailure("原配置的 AI 字段重复，请先核对。")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    if set(values) != allowed or not all(values.values()):
        raise ConfigFailure("原配置缺少所需的 AI 字段。")
    base = values["LLM_PRIMARY_BASE_URL"].rstrip('/')
    if base == "https://api.deepseek.com":
        base += "/v1"
    return base, values["LLM_PRIMARY_MODEL"], values["LLM_PRIMARY_API_KEY"]
