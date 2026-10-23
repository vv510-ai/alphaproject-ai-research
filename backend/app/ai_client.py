"""One bounded official model call, with no fallback, tool calls or automatic retry."""
from dataclasses import dataclass
import asyncio
import json
import re
import time

import httpx

MAX_OUTPUT_TOKENS = 1800
MAX_REQUEST_BYTES = 24000
MAX_RESPONSE_BYTES = 128 * 1024
TOTAL_TIMEOUT_SECONDS = 45
INPUT_MICRO_PER_TOKEN = 2
OUTPUT_MICRO_PER_TOKEN = 8
PRICE_PROFILE = 'deepseek-flash-cn-peak-2026-10-18'


class AITransportFailure(Exception):
    def __init__(self, code, message, uncertain=False):
        self.code, self.message, self.uncertain = code, message, uncertain
        super().__init__(message)


@dataclass(frozen=True)
class Completion:
    content: str
    model: str
    fingerprint: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    finish_reason: str
    elapsed_ms: int


def input_upper_bound(messages):
    # UTF-8 byte count plus protocol margin is intentionally conservative. The
    # saved value is a budget estimate, not the provider's invoice/tokenizer.
    raw = json.dumps(messages, ensure_ascii=False).encode()
    if len(raw) > MAX_REQUEST_BYTES:
        raise AITransportFailure('input_too_large', '这份事件依据超出本课的输入范围，未调用模型。')
    return len(raw) + 1024


def reservation(messages):
    return input_upper_bound(messages) * INPUT_MICRO_PER_TOKEN + MAX_OUTPUT_TOKENS * OUTPUT_MICRO_PER_TOKEN


class DeepSeekClient:
    def __init__(self, config, transport=None):
        self.config, self.transport = config, transport

    def _client(self):
        return httpx.Client(base_url=self.config.base_url,
            headers={'Authorization': 'Bearer ' + self.config.api_key},
            timeout=httpx.Timeout(35, connect=5, write=8, pool=2),
            follow_redirects=False, trust_env=False, transport=self.transport)

    def check_model(self):
        try:
            with self._client() as client:
                response = client.get('models')
                if response.status_code != 200:
                    raise AITransportFailure('model_unavailable', '模型身份核对未通过，请检查现有配置与账号状态。')
                models = response.json()['data']
                if not any(item.get('id') == self.config.model for item in models):
                    raise ValueError()
                return {'available': True, 'model': self.config.model}
        except AITransportFailure:
            raise
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            raise AITransportFailure('model_unavailable', '暂时无法核对模型身份；没有生成解释。') from None

    def complete(self, messages):
        try:
            return asyncio.run(asyncio.wait_for(self._complete(messages), TOTAL_TIMEOUT_SECONDS))
        except TimeoutError:
            raise AITransportFailure('request_deadline', '模型请求超过本次总等待上限，结果未采用；用量未知，不会自动重试。', True) from None

    async def _complete(self, messages):
        input_upper_bound(messages)
        started = time.monotonic()
        payload = {'model': self.config.model, 'messages': messages,
                   'thinking': {'type': 'disabled'}, 'temperature': 0.2,
                   'max_tokens': MAX_OUTPUT_TOKENS, 'stream': False,
                   'response_format': {'type': 'json_object'}}
        try:
            async with httpx.AsyncClient(base_url=self.config.base_url,
                    headers={'Authorization': 'Bearer ' + self.config.api_key},
                    timeout=httpx.Timeout(35, connect=5, write=8, pool=2),
                    follow_redirects=False, trust_env=False, transport=self.transport) as client, \
                    client.stream('POST', 'chat/completions', json=payload) as response:
                if response.status_code != 200:
                    status = response.status_code
                    labels = {401: 'AI 配置未通过认证，请重新核对。', 402: '模型账户额度不足，请核对账户后再手动重试。',
                              403: '模型服务拒绝了本次请求。', 429: '模型服务暂时限流，请稍后手动重试。'}
                    raise AITransportFailure('provider_rejected' if status < 500 else 'provider_unknown',
                        labels.get(status, '模型服务未完成本次请求，请稍后核对。'), status >= 500)
                parts, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise AITransportFailure('response_limit', '模型回复超出本次大小或等待上限，结果未采用。', True)
                    parts.append(chunk)
                data = json.loads(b''.join(parts))
            choice = data['choices'][0]
            message = choice['message']
            content = message.get('content')
            if message.get('tool_calls') or not isinstance(content, str) or not content.strip() or len(content) > 20000:
                raise ValueError()
            model = data['model']
            if not isinstance(model, str) or not (model == self.config.model or re.fullmatch(r'deepseek-v4\.1-flash(?:-\d{4})?', model)):
                raise ValueError()
            usage = data.get('usage') or {}
            prompt, completion = usage.get('prompt_tokens'), usage.get('completion_tokens')
            if type(prompt) is not int or type(completion) is not int or prompt < 0 or completion < 0:
                prompt = completion = None
            fingerprint = data.get('system_fingerprint')
            if not isinstance(fingerprint, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', fingerprint):
                fingerprint = None
            finish = choice['finish_reason']
            if finish not in ('stop', 'length', 'content_filter', 'tool_calls', 'insufficient_system_resource', 'aborted'):
                raise ValueError()
            return Completion(content, model, fingerprint, prompt, completion, finish,
                              int((time.monotonic() - started) * 1000))
        except AITransportFailure:
            raise
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            raise AITransportFailure('not_connected', '未能连接模型服务；本次没有取得解释，可稍后手动重试。') from None
        except httpx.HTTPError:
            raise AITransportFailure('request_unknown', '模型请求中断或超时，是否产生用量暂不确定；不会自动重试。', True) from None
        except (ValueError, KeyError, TypeError, IndexError):
            raise AITransportFailure('invalid_response', '模型响应为空或无法核对，结果未采用；不会自动重试。', True) from None
