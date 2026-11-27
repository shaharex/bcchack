import json
from contextlib import nullcontext
from unittest.mock import Mock

import httpx
import pytest

from evals import run
from requirements_validator import __main__ as cli
from requirements_validator.mistral import MistralClient, MistralSettings
from requirements_validator.ports import LLMError, StructuredResponseError
from requirements_validator.reviewers import FocusedReview, SpecificationReview


def completion(content, *, finish_reason="stop"):
    return httpx.Response(200, json={
        "choices": [{"finish_reason": finish_reason, "message": {"content": content}}],
        "usage": {"prompt_tokens": 17, "completion_tokens": 23},
    })


def test_mistral_sends_existing_instruction_payload_and_schema_with_high_reasoning():
    requests = []

    def handle(request):
        requests.append(request)
        return completion([
            {"type": "thinking", "thinking": [{"type": "text", "text": "This is not JSON."}]},
            {"type": "text", "text": '{"outcome":"pass",'},
            {"type": "text", "text": '"rationale":"Описание согласовано.","conflicts":[]}'},
        ])

    settings = MistralSettings.from_env({"MISTRAL_API_KEY": "offline", "MISTRAL_TIMEOUT_MS": "4500"})
    with MistralClient(settings, transport=httpx.MockTransport(handle)) as client:
        assert client.generate("Exact instruction\n", '{"text":"Расходы"}', FocusedReview).outcome == "pass"
        assert client.usage == {"prompt_tokens": 17, "completion_tokens": 23}
    request, = requests
    assert str(request.url) == "https://api.mistral.ai/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer offline"
    assert request.extensions["timeout"]["read"] == 4.5
    body = json.loads(request.content)
    assert body["model"] == "mistral-large-4-0"
    assert body["reasoning_effort"] == "high"
    assert body["max_tokens"] == settings.max_tokens
    assert body["messages"] == [{"role": "system", "content": "Exact instruction\n"},
                                {"role": "user", "content": '{"text":"Расходы"}'}]
    assert body["response_format"] == {"type": "json_schema", "json_schema": {
        "name": "FocusedReview", "schema": FocusedReview.model_json_schema(), "strict": True,
    }}


def test_mistral_honors_retry_after_and_wait_budget():
    responses = iter([httpx.Response(429, headers={"Retry-After": "3"}),
                      httpx.Response(429, headers={"Retry-After": "4"})])
    sleeps = []
    settings = MistralSettings(api_key="offline", max_retry_wait_seconds=6)
    with MistralClient(settings, transport=httpx.MockTransport(lambda request: next(responses)),
                       sleep=sleeps.append) as client:
        with pytest.raises(LLMError, match="HTTP 429.*attempts=2.*retry after 4s"):
            client.generate("instruction", "{}", SpecificationReview)
    assert sleeps == [3]


def test_mistral_reports_model_tier_denial_without_retrying():
    response = httpx.Response(403, json={"type": "tier_not_allowed", "message": "private service details"})
    sleeps = []
    with MistralClient(MistralSettings(api_key="offline"),
                       transport=httpx.MockTransport(lambda request: response), sleep=sleeps.append) as client:
        with pytest.raises(LLMError, match=r"HTTP 403, subscription tier does not allow mistral-large-4-0") as error:
            client.generate("instruction", "{}", SpecificationReview)
    assert "private service details" not in str(error.value)
    assert not sleeps


@pytest.mark.parametrize("response,error", [
    (completion('{}'), StructuredResponseError),
    (completion('{"findings":[]}', finish_reason="length"), StructuredResponseError),
    (completion([{"type": "thinking", "thinking": []}]), StructuredResponseError),
    (httpx.Response(401, text="private service details"), LLMError),
])
def test_mistral_fails_safely_on_invalid_responses(response, error):
    sleeps = []
    with MistralClient(MistralSettings(api_key="offline"),
                       transport=httpx.MockTransport(lambda request: response), sleep=sleeps.append) as client:
        with pytest.raises(error) as failure:
            client.generate("instruction", "{}", SpecificationReview)
    assert "private service details" not in str(failure.value)
    assert not sleeps


def test_evals_use_mistral_with_the_same_review_stages(monkeypatch):
    schemas = []

    def handle(request):
        body = json.loads(request.content)
        schema = body["response_format"]["json_schema"]["name"]
        schemas.append(schema)
        payload = json.loads(body["messages"][1]["content"])
        assert payload["document"]["title"] == "Спецификация требований"
        assert case.id not in body["messages"][1]["content"]
        responses = {
            "SpecificationReview": {"findings": []},
            "DependencyReview": {"dependency_gaps": []},
            "FocusedReview": {"outcome": "not_assessable", "rationale": "Нет сценария.", "conflicts": []},
            "LifecycleReview": {"outcome": "not_applicable", "rationale": "Нет стадий.", "checks": []},
            "RelationReviews": {"rows": [{"link": 0, "case": "Нет связи.", "first": None, "second": None,
                                          "outcome": "unrelated", "subject": None, "consequence": None,
                                          "correction": None}]},
        }
        return completion(json.dumps(responses[schema], ensure_ascii=False))

    case = run.OFFICIAL_REGRESSION_CASES[0]
    monkeypatch.setattr(run, "_live_env", lambda: {"MISTRAL_API_KEY": "offline"})
    monkeypatch.setattr(run, "MistralClient", lambda settings: MistralClient(
        settings, transport=httpx.MockTransport(handle)))
    report, complete = run.run_live((case,), provider="mistral")
    assert complete
    assert schemas == ["SpecificationReview", "DependencyReview", *(["FocusedReview"] * 5),
                       "LifecycleReview", "RelationReviews"]
    assert report["model"] == "mistral-large-4-0" and report["reasoning_effort"] == "high"
    assert report["usage"] == {"prompt_tokens": 17 * len(schemas), "completion_tokens": 23 * len(schemas)}
    assert report["summary"]["automated_expected"] == 1


def test_cli_mistral_uses_its_own_checkpoint_and_credentials(tmp_path, monkeypatch, capsys):
    source = tmp_path / "requirements.md"
    source.write_text("# Требования\n\nСервис сохраняет порог.\n", encoding="utf-8")
    output = tmp_path / "report.md"
    gemini_checkpoint = tmp_path / ".report.md.gemini-checkpoint.sqlite3"
    gemini_checkpoint.write_bytes(b"existing Gemini checkpoint")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MISTRAL_API_KEY", "offline")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    llm = Mock(model="mistral-large-4-0")
    llm.generate.side_effect = LLMError("test provider failure")
    mock_mistral = Mock(return_value=nullcontext(llm))
    mock_gemini = Mock()
    monkeypatch.setattr(cli, "MistralClient", mock_mistral)
    monkeypatch.setattr(cli, "GeminiClient", mock_gemini)
    assert cli.main(["--provider", "mistral", "--input", str(source), "--output", str(output)]) == 1
    mock_mistral.assert_called_once()
    mock_gemini.assert_not_called()
    assert "Mistral analysis failed" in capsys.readouterr().err
    assert (tmp_path / ".report.md.mistral-checkpoint.sqlite3").exists()
    assert gemini_checkpoint.read_bytes() == b"existing Gemini checkpoint"
