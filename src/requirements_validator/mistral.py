import httpx
from pydantic import Field

from .chat_completions import ChatCompletionsClient, ChatCompletionsSettings
from .models import Text
from .ports import T

REASONING_EFFORT = "high"
MISTRAL_ENV = {
    "api_key": "MISTRAL_API_KEY", "model": "MISTRAL_MODEL", "timeout_ms": "MISTRAL_TIMEOUT_MS",
    "max_attempts": "MISTRAL_MAX_ATTEMPTS", "max_retry_wait_seconds": "MISTRAL_MAX_RETRY_WAIT_SECONDS",
    "max_tokens": "MISTRAL_MAX_TOKENS",
}


class MistralSettings(ChatCompletionsSettings):
    env_names = MISTRAL_ENV

    model: Text = "mistral-large-4-0"
    timeout_ms: int = Field(default=300_000, gt=0)


def _tier_denied(response: httpx.Response) -> bool:
    try:
        data = response.json()
    except ValueError:
        return False
    return isinstance(data, dict) and data.get("type") == "tier_not_allowed"


class MistralClient(ChatCompletionsClient):
    provider_name = "Mistral"
    base_url = "https://api.mistral.ai/v1/"

    def _request_options(self, schema: type[T]) -> dict[str, object]:
        return {
            "reasoning_effort": REASONING_EFFORT,
            "max_tokens": self.settings.max_tokens,
            "response_format": {"type": "json_schema", "json_schema": {
                "name": schema.__name__, "schema": schema.model_json_schema(), "strict": True,
            }},
        }

    def _forbidden_detail(self, response: httpx.Response) -> str:
        if _tier_denied(response):
            return f", subscription tier does not allow {self.model}"
        return ""
