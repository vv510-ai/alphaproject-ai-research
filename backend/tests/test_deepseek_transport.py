"""Exercise the SDK wire format offline, without real credentials or billing."""
import json

import httpx
import openai
import pytest

from app.services.llm_service import OpenAICompatClient


@pytest.mark.parametrize("base_url,model,flash", [
    ("https://api.deepseek.com/v1", "deepseek-flash", True),
    ("https://api.deepseek.com/v1", "deepseek-v4-pro", False),
    ("https://example.test/v1", "other-model", False),
])
def test_flash_uses_bounded_nonthinking_request(monkeypatch, base_url, model, flash):
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "offline", "object": "chat.completion", "created": 1, "model": model,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "测试正文"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        })

    real_client = openai.OpenAI
    def factory(**kwargs):
        assert kwargs["max_retries"] == 0
        return real_client(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(respond)))

    monkeypatch.setattr(openai, "OpenAI", factory)
    client = OpenAICompatClient(api_key="offline-test-key", base_url=base_url)
    try:
        result = client.chat_complete(model=model, messages=[{"role": "user", "content": "测试"}], timeout=45)
    finally:
        client._client.close()
    assert result == {"text": "测试正文", "prompt_tokens": 100, "completion_tokens": 20}
    assert requests[0]["model"] == model
    if flash:
        assert requests[0]["thinking"] == {"type": "disabled"}
        assert requests[0]["max_tokens"] == 2048
    else:
        assert "thinking" not in requests[0]

