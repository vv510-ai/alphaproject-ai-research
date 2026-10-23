"""Small, user-scoped Windows vault. No secret values in repr or errors."""

import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field, asdict
import hashlib
import json
import os
from pathlib import Path
import re
import stat


class ConfigFailure(Exception):
    pass


def credential_permissions(mode,owner,group,current_user,directory,name=None):
    acceptance = Path(directory)==Path('/run/credentials/alphaproject-lab-acceptance.service')
    if acceptance and name != 'feishu.json':
        return False
    if owner not in (0,current_user) or mode & 0o037:
        return False
    # systemd 255 supplies 0440 root-owned files behind a per-service ACL/mount.
    # Only accept this group-readable form at this service's protected runtime path.
    protected = Path(directory)==Path('/run/credentials/alphaproject-lab.service') or acceptance
    return not (mode & 0o040) or (owner==group==0 and protected)


def service_credential(name: str):
    """Read a systemd credential, outside releases and never from request input."""
    directory = os.environ.get('CREDENTIALS_DIRECTORY')
    if not directory:
        return None
    if name not in ('feishu.json', 'ai.json') or not Path(directory).is_absolute():
        raise ConfigFailure('服务凭据目录或名称不正确。')
    path = Path(directory)/name
    try:
        info = path.stat()
        if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
            raise ValueError()
        if os.name == 'posix' and not credential_permissions(info.st_mode,info.st_uid,info.st_gid,os.geteuid(),directory,name):
            raise ValueError()
        value = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (OSError, ValueError):
        raise ConfigFailure('服务凭据缺失、格式或权限不正确，请核对部署配置。') from None


@dataclass(frozen=True)
class FeishuConfig:
    app_id: str = field(repr=False)
    app_secret: str = field(repr=False)
    chat_id: str = field(repr=False)
    owner_id: str = field(repr=False)
    group_name: str
    bot_name: str
    imported_at: str

    @property
    def fingerprint(self):
        return hashlib.sha256((self.app_id + "|" + self.chat_id + "|" + self.owner_id).encode()).hexdigest()[:24]


def _crypt(data: bytes, decrypt=False, *, purpose="飞书") -> bytes:
    if os.name != "nt":
        raise ConfigFailure("当前配置使用 Windows 用户加密，请在这台电脑的原 Windows 用户下运行。")

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    library = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        function = library.CryptUnprotectData
        function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        description = None
    else:
        function = library.CryptProtectData
        function.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        description = f"AlphaProject Lab {purpose}"
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), description, None, None, None, 1, ctypes.byref(result)):
        raise ConfigFailure(f"{purpose}配置无法加密或解密，请用原 Windows 用户重新导入配置。")
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel.LocalFree(ctypes.cast(result.data, ctypes.c_void_p))


def save_config(path: Path, config: FeishuConfig):
    encrypted = _crypt(json.dumps(asdict(config), ensure_ascii=False).encode())
    # Verify usability before replacing the existing file.
    if _crypt(encrypted, True) != json.dumps(asdict(config), ensure_ascii=False).encode():
        raise ConfigFailure("加密配置回读不一致，未保存。")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(encrypted)
    os.replace(temporary, path)


def load_config(path: Path) -> FeishuConfig | None:
    credential = service_credential('feishu.json')
    if credential is None and not path.is_file():
        return None
    try:
        values = credential if credential is not None else json.loads(_crypt(path.read_bytes(), True))
        result = FeishuConfig(**values)
        if not all(isinstance(value, str) and value for value in values.values()):
            raise ValueError()
        return result
    except (OSError, ValueError, TypeError) as error:
        raise ConfigFailure("飞书配置无法读取，请核对当前运行环境的凭据。") from None


def read_source_fields(path: Path) -> dict:
    allowed = {"LARK_APP_ID", "LARK_APP_SECRET", "LARK_RECEIVE_ID", "LARK_RECEIVE_ID_TYPE"}
    result = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        match = re.match(r"^\s*([A-Z_]+)\s*=\s*(.*?)\s*$", line)
        if not match or match[1] not in allowed:
            continue
        key, value = match.groups()
        if key in result:
            raise ConfigFailure("原配置存在重复的飞书字段，请先核对。")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        result[key] = value
    if any(not result.get(key) for key in allowed) or result["LARK_RECEIVE_ID_TYPE"] != "chat_id":
        raise ConfigFailure("原配置缺少飞书字段，或接收目标不是群。")
    return result
