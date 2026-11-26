import logging
import os
import re
import time
from collections.abc import Callable, Mapping
from types import TracebackType
from typing import Self

import httpx
from google import genai
from google.genai import errors, types
from pydantic import Field, SecretStr, ValidationError, field_validator

from .models import Schema, Text
from .ports import LLMError as LLMError, StructuredResponseError, T

LOG = logging.getLogger(__name__)


def retry_after(error: errors.APIError) -> float:
    # Gemini's quota response can carry a protobuf Duration even without a header.
    header = error.response.headers.get("Retry-After", "") if error.response is not None else ""
    delays = [float(header)] if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", header) else []
    body = error.details.get("error", {}) if isinstance(error.details, dict) else {}
    for item in body.get("details", []) if isinstance(body, dict) else ():
        if isinstance(item, dict) and item.get("@type") == "type.googleapis.com/google.rpc.RetryInfo":
            delay = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)s", str(item.get("retryDelay")))
            if delay:
                delays.append(float(delay[1]))
    return max(delays, default=0)


class GeminiSettings(Schema):
    api_key: SecretStr
    model: Text = "gemini-flash-latest"
    timeout_ms: int = Field(default=60_000, gt=0)
    max_attempts: int = Field(default=3, ge=1, le=8)
    max_retry_wait_seconds: int = Field(default=60, ge=0)

    @field_validator("api_key")
    @classmethod
    def valid_key(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("GEMINI_API_KEY is required")
        return value

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Self:
        env = os.environ if env is None else env
        names = {"api_key": "GEMINI_API_KEY", "model": "GEMINI_MODEL",
                 "timeout_ms": "GEMINI_TIMEOUT_MS", "max_attempts": "GEMINI_MAX_ATTEMPTS",
                 "max_retry_wait_seconds": "GEMINI_MAX_RETRY_WAIT_SECONDS"}
        return cls.model_validate({key: env[name] for key, name in names.items() if name in env})


class GeminiClient:
    def __init__(self, settings: GeminiSettings, *, client: genai.Client | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.settings = settings
        self._owns_client = client is None
        self._client = client if client is not None else genai.Client(
            vertexai=False, api_key=settings.api_key.get_secret_value()
        )
        self._sleep = sleep

    @property
    def model(self) -> str:
        return self.settings.model

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 tb: TracebackType | None) -> None:
        self.close()

    def generate(self, instruction: str, payload: str, schema: type[T]) -> T:
        config = types.GenerateContentConfig(
            system_instruction=instruction,
            response_mime_type="application/json",
            response_json_schema=schema.model_json_schema(),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            http_options=types.HttpOptions(timeout=self.settings.timeout_ms,
                                          retry_options=types.HttpRetryOptions(attempts=1)),
        )
        wait_remaining = self.settings.max_retry_wait_seconds
        for attempt in range(1, self.settings.max_attempts + 1):
            try:
                response = self._client.models.generate_content(
                    model=self.model, contents=payload, config=config
                )
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError,
                    errors.APIError) as exc:
                code = exc.code if isinstance(exc, errors.APIError) else None
                transient = code is None or code in {408, 429, 500, 502, 503, 504}
                requested = retry_after(exc) if transient and isinstance(exc, errors.APIError) else 0
                delay = max(requested, min(2 ** (attempt - 1), 16))
                if transient and attempt < self.settings.max_attempts and delay <= wait_remaining:
                    if code in {429, 503}:
                        LOG.info("Gemini returned HTTP %d for %s. Retrying in %.1fs.", code, schema.__name__, delay)
                    self._sleep(delay)
                    wait_remaining -= delay
                    continue
                failure = f"HTTP {code}" if code is not None else type(exc).__name__
                advice = ""
                if code == 429:
                    failure += ", rate limit or quota exceeded"
                    advice = (f" Check the Google AI Studio limits for project and model {self.model}, "
                              "then retry when quota is available or use a project with sufficient quota.")
                if requested:
                    advice = f" Gemini requested a retry after {requested:g}s." + advice
                raise LLMError(f"Gemini request failed for {schema.__name__} ({failure}, attempts={attempt})."
                               + advice) from exc
            except ValueError as exc:
                # SDK request conversion and response-envelope decoding both
                # precede validation of the model's generated JSON below.
                raise LLMError("Gemini SDK could not encode the request or decode the response") from exc

            finish = response.candidates[0].finish_reason if response.candidates else None
            if finish == types.FinishReason.MAX_TOKENS:
                raise StructuredResponseError(f"Gemini truncated the response for {schema.__name__}")
            if finish != types.FinishReason.STOP:
                raise LLMError("Gemini returned a blocked or incomplete response")
            raw = response.text
            if not raw:
                raise StructuredResponseError(f"Gemini returned no JSON text for {schema.__name__}")
            try:
                return schema.model_validate_json(raw, strict=True)
            except ValidationError as exc:
                raise StructuredResponseError(f"Invalid structured response for {schema.__name__}") from exc
        raise AssertionError("max_attempts must be positive")
