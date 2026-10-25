"""LLMClient behaviour with a stubbed SDK client: repair retry, errors, usage and cost."""

from __future__ import annotations

from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from pydantic import BaseModel

from app.config import Settings
from app.llm_client import LLMClient, LLMError


class Capital(BaseModel):
    capital: str


def _response(parsed: Capital | None, stop_reason: str = "end_turn") -> SimpleNamespace:
    usage = SimpleNamespace(input_tokens=1000, output_tokens=100)
    return SimpleNamespace(parsed_output=parsed, stop_reason=stop_reason, usage=usage)


class StubSDK:
    def __init__(self, *outcomes: object) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[dict] = []
        self.messages = SimpleNamespace(parse=self._parse)

    def _parse(self, **kwargs: object) -> object:
        self.requests.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def cfg(settings: Settings) -> Settings:
    settings.llm_model, settings.llm_model_fast = "claude-sonnet-5-5", "claude-haiku-5-5"
    return settings


def test_returns_parsed_output_and_tracks_cost(cfg: Settings) -> None:
    sdk = StubSDK(_response(Capital(capital="Paris")))
    llm = LLMClient(cfg, client=sdk)
    assert llm.structured("q", Capital, system="s", role="strong", agent="t") == Capital(capital="Paris")
    request = sdk.requests[0]
    assert request["model"] == "claude-sonnet-5-5" and request["output_format"] is Capital
    assert request["output_config"] == {"effort": cfg.llm.effort}
    assert "temperature" not in request  # rejected by the API since 2026-10-21
    assert llm.usage.cost_usd == pytest.approx((1000 * 2.0 + 100 * 10.0) / 1e6)
    assert llm.trace.events[-1]["llm"]["in_tokens"] == 1000


def test_unusable_reply_is_retried_once_with_reason(cfg: Settings) -> None:
    sdk = StubSDK(_response(None, stop_reason="max_tokens"), _response(Capital(capital="Paris")))
    llm = LLMClient(cfg, client=sdk)
    assert llm.structured("q", Capital, system="s", role="fast", agent="t").capital == "Paris"
    assert "could not be used (no usable output (stop_reason=max_tokens))" in sdk.requests[1]["messages"][0]["content"]
    assert llm.usage.calls == 2


def test_second_bad_reply_raises(cfg: Settings) -> None:
    llm = LLMClient(cfg, client=StubSDK(ValueError("bad json"), ValueError("bad json")))
    with pytest.raises(LLMError, match="did not match the schema"):
        llm.structured("q", Capital, system="s", role="fast", agent="t")
    assert llm.usage.failures == 2


def test_api_errors_and_refusals_raise_llm_error(cfg: Settings) -> None:
    error = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com"))
    with pytest.raises(LLMError, match="APIConnectionError"):
        LLMClient(cfg, client=StubSDK(error)).structured("q", Capital, system="s", role="fast", agent="t")
    with pytest.raises(LLMError, match="declined"):
        LLMClient(cfg, client=StubSDK(_response(None, "refusal"))).structured(
            "q", Capital, system="s", role="fast", agent="t"
        )


def test_missing_configuration(cfg: Settings) -> None:
    cfg.anthropic_api_key = None
    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY"):
        LLMClient(cfg)
    cfg.llm_model_fast = None
    with pytest.raises(LLMError, match="LLM_MODEL_FAST"):
        LLMClient(cfg, client=StubSDK()).model_for("fast")
