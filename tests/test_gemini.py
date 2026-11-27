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


def invoke(responses, requests, sleeps, *, max_attempts=2, max_wait=60):
    def handler(request):
        requests.append(request)
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    settings = GeminiSettings.from_env({"GEMINI_API_KEY": "unit-test", "GEMINI_MODEL": "test-model",
                                        "GEMINI_TIMEOUT_MS": "4500", "GEMINI_MAX_ATTEMPTS": str(max_attempts),
                                        "GEMINI_MAX_RETRY_WAIT_SECONDS": str(max_wait)})
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
        [quota_failure(header="2", protobuf_delay="5s"), success()], requests, sleeps,
    )
    assert result.findings == ()
    assert len(requests) == 2
    assert sleeps == [5]


def test_sdk_transport_stops_on_long_quota_reset():
    requests, sleeps = [], []
    with pytest.raises(LLMError, match=r"HTTP 429.*retry after 48703s.*AI Studio"):
        invoke([quota_failure(header="1", protobuf_delay="48703s")], requests, sleeps)
    assert len(requests) == 1
    assert sleeps == []


def test_short_retries_share_the_wait_budget():
    requests, sleeps = [], []
    with pytest.raises(LLMError, match=r"HTTP 429"):
        invoke([quota_failure(header="4"), quota_failure(header="4")], requests, sleeps,
               max_attempts=3, max_wait=6)
    assert len(requests) == 2
    assert sleeps == [4]


def test_unavailable_model_switches_to_fallback_for_the_rest_of_the_run():
    requests, sleeps = [], []
    responses = [failure(503), failure(503), success(), success()]

    def handler(request):
        requests.append(request)
        return responses.pop(0)

    settings = GeminiSettings.from_env({"GEMINI_API_KEY": "unit-test", "GEMINI_MODEL": "primary",
                                        "GEMINI_MAX_ATTEMPTS": "2",
                                        "GEMINI_FALLBACK_MODELS": " backup , primary,"})
    assert settings.fallback_models == ("backup", "primary")
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with genai.Client(api_key="unit-test", http_options=types.HttpOptions(httpx_client=http)) as sdk:
            client = GeminiClient(settings, client=sdk, sleep=sleeps.append)
            assert client.model == "primary"
            client.generate("review", "document", SpecificationReview)
            client.generate("review", "document", SpecificationReview)
    assert ["primary" in str(r.url) for r in requests] == [True, True, False, False]
    assert all("backup" in str(r.url) for r in requests[2:])
    assert client.model == "primary (резерв: backup)"


def test_permanent_error_does_not_switch_models():
    requests = []

    def handler(request):
        requests.append(request)
        return failure(403)

    settings = GeminiSettings.from_env({"GEMINI_API_KEY": "unit-test", "GEMINI_MODEL": "primary",
                                        "GEMINI_FALLBACK_MODELS": "backup"})
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with genai.Client(api_key="unit-test", http_options=types.HttpOptions(httpx_client=http)) as sdk:
            with pytest.raises(LLMError, match="HTTP 403"):
                GeminiClient(settings, client=sdk, sleep=lambda _: None).generate("r", "d", SpecificationReview)
    assert len(requests) == 1
