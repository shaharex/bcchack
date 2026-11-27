import logging
import os
import time
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from types import TracebackType
from typing import ClassVar, Self

import httpx
from pydantic import Field, SecretStr, ValidationError, field_validator

from .models import Schema, Text
from .ports import LLMError, StructuredResponseError, T

LOG = logging.getLogger(__name__)


class ChatCompletionsSettings(Schema):
    env_names: ClassVar[Mapping[str, str]]

    api_key: SecretStr
    model: Text
    timeout_ms: int = Field(default=60_000, gt=0)
    max_attempts: int = Field(default=3, ge=1, le=8)
    max_retry_wait_seconds: int = Field(default=60, ge=0)
    max_tokens: int = Field(default=16_384, gt=0)

    @field_validator("api_key")
    @classmethod
    def valid_key(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError(f"{cls.env_names['api_key']} is required")
        return value

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Self:
        env = os.environ if env is None else env
        return cls.model_validate({key: env[name] for key, name in cls.env_names.items() if name in env})


def _retry_after(response: httpx.Response) -> float:
    value = response.headers.get("Retry-After", "")
    if value.isascii() and value.isdecimal():
        return float(value)
    try:
        return max(0.0, parsedate_to_datetime(value).timestamp() - datetime.now(UTC).timestamp())
    except (ValueError, TypeError, OverflowError, OSError):
        return 0.0


class ChatCompletionsClient:
    provider_name: str
    base_url: str

    def __init__(self, settings: ChatCompletionsSettings, *, transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.settings = settings
        self.usage: Counter[str] = Counter()
        self._http = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {settings.api_key.get_secret_value()}"},
            timeout=settings.timeout_ms / 1000, transport=transport,
        )
        self._sleep = sleep

    @property
    def model(self) -> str:
        return self.settings.model

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 tb: TracebackType | None) -> None:
        self._http.close()

    def _request_options(self, schema: type[T]) -> dict[str, object]:
        raise NotImplementedError

    def _forbidden_detail(self, response: httpx.Response) -> str:
        return ""

    def _invalid_generation(self, response: httpx.Response) -> bool:
        return False

    def generate(self, instruction: str, payload: str, schema: type[T]) -> T:
        request = {
            "model": self.model,
            "messages": [{"role": "system", "content": instruction}, {"role": "user", "content": payload}],
            **self._request_options(schema),
        }
        wait_remaining = self.settings.max_retry_wait_seconds
        for attempt in range(1, self.settings.max_attempts + 1):
            try:
                response = self._http.post("chat/completions", json=request)
                response.raise_for_status()
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                requested = 0.0
                error_type = LLMError
                failure = type(exc).__name__
                transient = isinstance(exc, (httpx.NetworkError, httpx.TimeoutException, httpx.RemoteProtocolError))
                if isinstance(exc, httpx.HTTPStatusError):
                    status = exc.response.status_code
                    failure = f"HTTP {status}"
                    transient = status in {408, 429, 500, 502, 503, 504}
                    if status == 400 and self._invalid_generation(exc.response):
                        error_type = StructuredResponseError
                        failure += ", invalid structured output"
                        transient = True
                    if transient:
                        requested = _retry_after(exc.response)
                    if status == 429:
                        failure += ", rate limit or quota exceeded"
                    if status == 403:
                        failure += self._forbidden_detail(exc.response)
                delay = max(requested, min(2 ** (attempt - 1), 16))
                if transient and attempt < self.settings.max_attempts and delay <= wait_remaining:
                    LOG.warning("%s returned %s for %s. Retrying in %.1fs.",
                                self.provider_name, failure, schema.__name__, delay)
                    self._sleep(delay)
                    wait_remaining -= delay
                    continue
                hint = f" {self.provider_name} requested a retry after {requested:g}s." if requested else ""
                raise error_type(
                    f"{self.provider_name} request failed for {schema.__name__} ({failure}, attempts={attempt}).{hint}"
                ) from exc

            try:
                data = response.json()
            except ValueError as exc:
                raise LLMError(f"{self.provider_name} returned an invalid response envelope") from exc
            if not isinstance(data, dict):
                raise LLMError(f"{self.provider_name} returned an invalid response envelope")
            usage = data.get("usage")
            if isinstance(usage, dict):
                for name in ("prompt_tokens", "completion_tokens"):
                    count = usage.get(name)
                    if type(count) is int and count >= 0:
                        self.usage[name] += count
            choices = data.get("choices")
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                raise LLMError(f"{self.provider_name} returned no completion choice")
            choice = choices[0]
            if choice.get("finish_reason") in ("length", "model_length"):
                raise StructuredResponseError(f"{self.provider_name} truncated the response for {schema.__name__}")
            if choice.get("finish_reason") != "stop":
                raise LLMError(f"{self.provider_name} returned an incomplete response")
            message = choice.get("message")
            raw = message.get("content") if isinstance(message, dict) else None
            if isinstance(raw, list):
                raw = "".join(part["text"] for part in raw if isinstance(part, dict)
                              and part.get("type") == "text" and isinstance(part.get("text"), str))
            if not isinstance(raw, str) or not raw.strip():
                raise StructuredResponseError(f"{self.provider_name} returned no JSON text for {schema.__name__}")
            try:
                return schema.model_validate_json(raw, strict=True)
            except ValidationError as exc:
                raise StructuredResponseError(f"Invalid structured response for {schema.__name__}") from exc
        raise AssertionError("max_attempts must be positive")
