"""ASR result URLs are provider data, not trusted download instructions."""
import httpx
import pytest

from app.config import Settings
from app.integrations.asr import AsrError, DashScopeAsrClient


def _client(handler):
    settings = Settings(
        environment="test",
        database_url="postgresql://postgres@127.0.0.1:1/x",
        model={"provider": "stub"},
        auth={"service_secret": "test-secret"},
        asr={"enabled": True, "api_key": "test-key"},
    )
    return DashScopeAsrClient(settings, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


@pytest.mark.parametrize("url", [
    "http://result.oss-cn-beijing.aliyuncs.com/x.json",
    "https://127.0.0.1/x.json",
    "https://aliyuncs.com.evil.example/x.json",
    "https://user:pass@result.oss-cn-beijing.aliyuncs.com/x.json",
    "https://result.oss-cn-beijing.aliyuncs.com:8443/x.json",
])
async def test_rejects_untrusted_result_url_before_fetch(url):
    called = False

    def handler(request):
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    client = _client(handler)
    with pytest.raises(AsrError, match="允许的 HTTPS 域名"):
        await client._fetch_transcription({"results": [{"transcription_url": url}]})
    assert not called


async def test_rejects_result_redirect():
    client = _client(lambda request: httpx.Response(302, headers={"location": "http://127.0.0.1/private"}))
    with pytest.raises(AsrError, match="不允许重定向"):
        await client._fetch_transcription({"results": [{"transcription_url": "https://result.aliyuncs.com/x"}]})


async def test_rejects_oversized_stream_even_without_content_length():
    client = _client(lambda request: httpx.Response(200, content=b"x" * (8 * 1024 * 1024 + 1)))
    with pytest.raises(AsrError, match="大小上限"):
        await client._fetch_transcription({"results": [{"transcription_url": "https://result.aliyuncs.com/x"}]})


async def test_rejects_invalid_result_json():
    client = _client(lambda request: httpx.Response(200, content=b"not json"))
    with pytest.raises(AsrError, match="合法 JSON"):
        await client._fetch_transcription({"results": [{"transcription_url": "https://result.aliyuncs.com/x"}]})
