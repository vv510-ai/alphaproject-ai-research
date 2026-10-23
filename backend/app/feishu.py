"""Bounded calls to the existing Feishu application bot; no automatic send retries."""

from dataclasses import dataclass
import json
import re
from threading import Lock
import time

import httpx

from .feishu_config import FeishuConfig


class FeishuFailure(Exception):
    def __init__(self, message, unknown=False):
        super().__init__(message)
        self.message, self.unknown = message, unknown


@dataclass(frozen=True)
class Target:
    group_name: str
    bot_name: str
    owner_id: str


class FeishuClient:
    def __init__(self, config: FeishuConfig, transport=None):
        self.config, self.transport = config, transport
        self._token, self._expires = "", 0.0
        self._lock = Lock()

    def _request(self, method, path, *, token=None, payload=None, sending=False, params=None):
        headers = {"Authorization": "Bearer " + token} if token else {}
        try:
            with httpx.Client(base_url="https://open.feishu.cn", timeout=httpx.Timeout(8, connect=4),
                              trust_env=False, follow_redirects=False, transport=self.transport) as client:
                with client.stream(method, path, headers=headers, json=payload, params=params) as response:
                    if response.status_code != 200:
                        raise FeishuFailure(f"飞书请求未确认（HTTP {response.status_code}），请先核对群内记录。", sending)
                    chunks, size = [], 0
                    deadline = time.monotonic() + 10
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > 131072 or time.monotonic() > deadline:
                            raise FeishuFailure("飞书返回过慢或内容过长，未取得可靠确认。", sending)
                        chunks.append(chunk)
                    result = json.loads(b"".join(chunks))
            if not isinstance(result, dict) or type(result.get("code")) is not int:
                raise FeishuFailure("飞书返回格式不完整，未取得可靠确认。", sending)
            if result["code"] != 0:
                raise FeishuFailure(f"飞书明确拒绝（错误码 {result['code']}），请检查应用权限与群状态。")
            return result
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            raise FeishuFailure("未能建立飞书连接，本次未提交消息。") from None
        except (httpx.HTTPError, ValueError, UnicodeError):
            raise FeishuFailure("未收到可靠回执，请先到群里核对；系统不会自动重发。", sending) from None

    def token(self):
        with self._lock:
            if self._token and time.monotonic() < self._expires:
                return self._token
            result = self._request("POST", "/open-apis/auth/v3/tenant_access_token/internal", payload={
                "app_id": self.config.app_id, "app_secret": self.config.app_secret,
            })
            token, expires = result.get("tenant_access_token"), result.get("expire")
            if not isinstance(token, str) or not token or type(expires) is not int or expires <= 60:
                raise FeishuFailure("飞书认证返回不完整，请检查应用配置。")
            self._token, self._expires = token, time.monotonic() + expires - 60
            return token

    def verify_target(self, allow_unbound=False):
        if not re.fullmatch(r"oc_[A-Za-z0-9]+", self.config.chat_id):
            raise FeishuFailure("接收群标识无效，请重新导入配置。")
        token = self.token()
        prefix = "/open-apis/im/v1/chats/" + self.config.chat_id
        group = self._request("GET", prefix, token=token, params={"user_id_type": "open_id"}).get("data", {})
        members = self._request("GET", prefix + "/members", token=token,
                                params={"member_id_type": "open_id", "page_size": 100}).get("data", {})
        bot = self._request("GET", "/open-apis/bot/v3/info", token=token).get("bot", {})
        if not all(isinstance(item, dict) for item in (group, members, bot)):
            raise FeishuFailure("群核对信息不完整，本次停止发送。")
        items = members.get("items")
        owner = group.get("owner_id")
        if (group.get("chat_type") != "private" or group.get("chat_mode") != "group"
                or str(group.get("user_count")) != "1" or str(group.get("bot_count")) != "1"
                or members.get("has_more") is not False or members.get("member_total") != 1
                or not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict)
                or not isinstance(owner, str) or not owner or items[0].get("member_id") != owner
                or (not allow_unbound and owner != self.config.owner_id)
                or bot.get("activate_status") != 2):
            raise FeishuFailure("接收群或成员发生变化，必须是原来的私有群、本人和一个启用的机器人；本次停止发送。")
        name, bot_name = group.get("name"), bot.get("app_name")
        if not isinstance(name, str) or not name or not isinstance(bot_name, str) or not bot_name:
            raise FeishuFailure("群或机器人名称缺失，本次停止发送。")
        return Target(name[:200], bot_name[:200], owner)

    def send_test(self, request_id: str, text: str, detail_url: str | None = None):
        content, kind = {'text': text}, 'text'
        if detail_url:
            access_note=('学习版公网入口：直接打开，无需电脑开机或同一 Wi-Fi。'
                         if detail_url.startswith('https://') else
                         '学习版入口：手机需能访问电脑所在局域网，电脑服务需保持运行。')
            content = {'zh_cn': {'title': 'AlphaProject Lab · 事件依据', 'content': [
                [{'tag': 'text', 'text': text}],
                [{'tag': 'a', 'text': '查看这次事件', 'href': detail_url}],
                [{'tag': 'text', 'text': access_note}],
            ]}}
            kind = 'post'
        result = self._request("POST", "/open-apis/im/v1/messages", token=self.token(), sending=True,
                               params={"receive_id_type": "chat_id"}, payload={
                                   "receive_id": self.config.chat_id, "msg_type": kind,
                                   "content": json.dumps(content, ensure_ascii=False), "uuid": request_id,
                               })
        data = result.get("data")
        message_id = data.get("message_id") if isinstance(data, dict) else None
        if not isinstance(message_id, str) or not re.fullmatch(r"om_[A-Za-z0-9]+", message_id):
            raise FeishuFailure("飞书返回成功但缺少消息标识，结果待核对，不会自动重发。", True)
        return message_id
