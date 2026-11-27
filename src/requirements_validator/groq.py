from typing import Any

import httpx

from .chat_completions import ChatCompletionsClient, ChatCompletionsSettings
from .models import Text
from .ports import T

GROQ_ENV = {
    "api_key": "GROQ_API_KEY", "model": "GROQ_MODEL", "timeout_ms": "GROQ_TIMEOUT_MS",
    "max_attempts": "GROQ_MAX_ATTEMPTS", "max_retry_wait_seconds": "GROQ_MAX_RETRY_WAIT_SECONDS",
    "max_tokens": "GROQ_MAX_COMPLETION_TOKENS",
}


class GroqSettings(ChatCompletionsSettings):
    env_names = GROQ_ENV

    model: Text = "openai/gpt-oss-20b"


def _require_all_properties(schema: dict[str, Any]) -> None:
    # Groq's strict decoding requires every object property, including fields with Pydantic defaults.
    schema.pop("default", None)
    if schema.get("type") == "object":
        properties = schema.get("properties", {})
        schema["additionalProperties"] = False
        schema["required"] = list(properties)
        for property_schema in properties.values():
            _require_all_properties(property_schema)
    for definition in schema.get("$defs", {}).values():
        _require_all_properties(definition)
    if "items" in schema:
        _require_all_properties(schema["items"])
    for branch in schema.get("anyOf", ()):
        _require_all_properties(branch)


class GroqClient(ChatCompletionsClient):
    provider_name = "Groq"
    base_url = "https://api.groq.com/openai/v1/"

    def _invalid_generation(self, response: httpx.Response) -> bool:
        try:
            data = response.json()
        except ValueError:
            return False
        error = data.get("error") if isinstance(data, dict) else None
        return isinstance(error, dict) and error.get("code") == "json_validate_failed"

    def _request_options(self, schema: type[T]) -> dict[str, object]:
        response_schema = schema.model_json_schema()
        _require_all_properties(response_schema)
        return {
            "max_completion_tokens": self.settings.max_tokens,
            "response_format": {"type": "json_schema", "json_schema": {
                "name": schema.__name__, "schema": response_schema, "strict": True,
            }},
        }
