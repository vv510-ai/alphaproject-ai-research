import requests

from app.integrations import akshare_adapter


def test_provider_requests_have_bounded_default_timeout(monkeypatch):
    calls = []
    monkeypatch.setattr(akshare_adapter, "_orig_session_request",
                        lambda _self, _method, _url, **kwargs: calls.append(kwargs))
    with requests.Session() as session:
        session.get("https://example.test")
        session.get("https://example.test", timeout=2)
    assert calls[0]["timeout"] == (5, 10)
    assert calls[1]["timeout"] == 2
