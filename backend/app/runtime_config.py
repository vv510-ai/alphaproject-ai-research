"""Non-secret deployment identity; never infer public URLs from request headers."""
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

APP_VERSION = '0.11.2'


def public_location():
    value = os.environ.get('ALPHA_LAB_PUBLIC_URL', '').rstrip('/')
    if not value:
        return None, '', None
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.port not in (None, 443)
            or not re.fullmatch(r'[A-Za-z0-9.-]+', parsed.hostname)
            or not re.fullmatch(r'(?:/[A-Za-z0-9_-]+)*', parsed.path)):
        raise RuntimeError('公网入口须为不含账号、查询参数的 HTTPS 地址。')
    return value, parsed.path, parsed.hostname


def release_info(root: Path):
    path = root/'release.json'
    if not path.is_file():
        return {'release_id': 'development', 'app_version': APP_VERSION}
    value = json.loads(path.read_text(encoding='utf-8'))
    if (not isinstance(value, dict) or value.get('app_version') != APP_VERSION
            or not re.fullmatch(r'lab-[A-Za-z0-9._-]{1,80}', value.get('release_id', ''))
            or not re.fullmatch(r'[a-f0-9]{64}', value.get('source_sha256', ''))):
        raise RuntimeError('发布标识与程序不一致，请重新部署经过校验的版本。')
    return {key: value[key] for key in ('release_id', 'app_version', 'source_sha256', 'built_at')}
