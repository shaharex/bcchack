import json

import httpx
import pytest
from google import genai
from google.genai import types
from pydantic import ValidationError

from requirements_validator.gemini import GeminiClient, GeminiSettings, LLMError
from requirements_validator.reviewers import SpecificationReview


def success(raw='{"findings":[]}', finish="STOP"):
    return httpx.Response(200, json={"candidates": [
        {"content": {"role": "model", "parts": [{"text": raw}]}, "finishReason": finish}
    ]})


def failure(status):
    return httpx.Response(status, json={"error": {"code": status, "message": "test error"}})


def quota_failure(*, header=None, protobuf_delay=None):
    details = [] if protobuf_delay is None else [{
        "@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": protobuf_delay,
    }]
    headers = {} if header is None else {"Retry-After": header}
    return httpx.Response(429, headers=headers, json={
        "error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED", "details": details},
    })


def invoke(responses, requests, sleeps):
    def handler(request):
        requests.append(request)
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    settings = GeminiSettings.from_env({"GEMINI_API_KEY": "unit-test", "GEMINI_MODEL": "test-model",
                                        "GEMINI_TIMEOUT_MS": "4500", "GEMINI_MAX_ATTEMPTS": "2"})
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with genai.Client(api_key=settings.api_key.get_secret_value(),
                          http_options=types.HttpOptions(httpx_client=http)) as sdk:
            client = GeminiClient(settings, client=sdk, sleep=sleeps.append)
            return client.generate("review", "document", SpecificationReview)


@pytest.mark.parametrize("first", [failure(429), failure(503), httpx.ReadTimeout("test timeout")])
def test_transient_retry_and_sdk_wire_request(first):
    requests, sleeps = [], []
    assert invoke([first, success()], requests, sleeps).findings == ()
    assert len(requests) == 2 and sleeps == [1]
    request = requests[0]
    assert "test-model" in str(request.url)
    assert request.extensions["timeout"]["read"] == 4.5
    config = json.loads(request.content)["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    assert "findings" in config["responseJsonSchema"]["properties"]


@pytest.mark.parametrize("response", [failure(403), success("not JSON"), success("{}"),
                                      success(finish="MAX_TOKENS"), httpx.Response(200, json={}),
                                      httpx.Response(200, text="malformed service response")])
def test_permanent_and_invalid_responses(response):
    requests, sleeps = [], []
    with pytest.raises(LLMError):
        invoke([response], requests, sleeps)
    assert len(requests) == 1 and sleeps == []


def test_retry_exhaustion_and_config():
    requests = []
    with pytest.raises(LLMError, match="attempts=2"):
        invoke([failure(503), failure(503)], requests, [])
    for env in ({}, {"GEMINI_API_KEY": "  "}, {"GEMINI_API_KEY": "x", "GEMINI_TIMEOUT_MS": "0"}):
        with pytest.raises(ValidationError):
            GeminiSettings.from_env(env)


def test_sdk_transport_honors_maximum_retry_after_from_header_and_retry_info():
    requests, sleeps = [], []
    result = invoke(
        [quota_failure(header="2", protobuf_delay="3.25s"), success()], requests, sleeps,
    )
    assert result.findings == ()
    assert len(requests) == 2
    assert sleeps == [3.25]


def test_sdk_transport_rejects_retry_info_beyond_configured_timeout():
    requests, sleeps = [], []
    with pytest.raises(LLMError, match=r"retry after 5s, beyond the configured timeout"):
        invoke([quota_failure(header="1", protobuf_delay="5s")], requests, sleeps)
    assert len(requests) == 1
    assert sleeps == []
