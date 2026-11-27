import json

import httpx
import pytest

from requirements_validator.extractor import Extraction
from requirements_validator.groq import GroqClient, GroqSettings
from requirements_validator.ports import LLMError, StructuredResponseError


def test_groq_sends_strict_schema_and_validates_completion():
    def handle(request):
        assert str(request.url) == "https://api.groq.com/openai/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer offline"
        body = json.loads(request.content)
        assert body["model"] == "openai/gpt-oss-20b"
        assert body["max_completion_tokens"] == 256
        assert body["messages"] == [{"role": "system", "content": "Extract requirements"},
                                    {"role": "user", "content": "{}"}]
        response_format = body["response_format"]
        assert response_format["type"] == "json_schema"
        schema = response_format["json_schema"]
        assert schema["name"] == "Extraction" and schema["strict"] is True
        root = schema["schema"]
        assert root["required"] == list(root["properties"])
        atom = root["$defs"]["Atom"]
        assert atom["additionalProperties"] is False
        assert "source" in atom["required"]
        assert {"type": "null"} in atom["properties"]["source"]["anyOf"]
        assert "default" not in atom["properties"]["source"]
        assert "default" not in root["properties"]["excluded"]
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": '{"groups":[],"excluded":[],"excluded_passages":[]}',
        }}]})

    settings = GroqSettings.from_env({"GROQ_API_KEY": "offline", "GROQ_MAX_COMPLETION_TOKENS": "256"})
    with GroqClient(settings, transport=httpx.MockTransport(handle)) as client:
        assert client.generate("Extract requirements", "{}", Extraction) == Extraction(groups=())


@pytest.mark.parametrize(("code", "expected", "delays"), [
    ("json_validate_failed", StructuredResponseError, [1]),
    ("invalid_request", LLMError, []),
])
def test_groq_retries_only_generation_errors_from_bad_requests(code, expected, delays):
    def handle(request):
        return httpx.Response(400, json={"error": {"code": code, "failed_generation": "private output"}})

    sleeps = []
    settings = GroqSettings(api_key="offline", max_attempts=2)
    with GroqClient(settings, transport=httpx.MockTransport(handle), sleep=sleeps.append) as client:
        with pytest.raises(expected, match="Groq request failed for Extraction") as exc:
            client.generate("Extract requirements", "{}", Extraction)
    assert type(exc.value) is expected
    assert "private output" not in str(exc.value)
    assert sleeps == delays
