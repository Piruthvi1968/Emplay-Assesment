"""Thin wrapper over the Anthropic SDK: schema-validated outputs, one repair retry, token/cost totals, tracing.

Every agent talks to Claude through `LLM.structured()`, which returns a validated Pydantic object or
raises `LLMError`. Callers catch `LLMError` and mark their work failed; nothing crashes the pipeline.
"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass
from typing import Literal, Protocol, TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

from app.config import Settings
from app.logging_utils import TraceLogger

ModelRole = Literal["strong", "fast"]
T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    """The call failed (network, API error, refusal) or returned output that does not fit the schema."""


class LLM(Protocol):
    """What agents depend on; `LLMClient` in the app, a fake in tests."""

    def structured(self, prompt: str, schema: type[T], *, system: str, role: ModelRole, agent: str) -> T: ...


@dataclass
class UsageTotals:
    calls: int = 0
    failures: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    def as_dict(self) -> dict[str, float]:
        return {**asdict(self), "cost_usd": round(self.cost_usd, 4)}


class LLMClient:
    def __init__(
        self, settings: Settings, trace: TraceLogger | None = None, client: anthropic.Anthropic | None = None
    ) -> None:
        if client is None and not settings.anthropic_api_key:
            raise LLMError("ANTHROPIC_API_KEY is not set; copy .env.example to .env and add a key")
        self.cfg = settings.llm
        self.models: dict[ModelRole, str | None] = {"strong": settings.llm_model, "fast": settings.llm_model_fast}
        self.trace = trace or TraceLogger()
        self.usage = UsageTotals()
        self._lock = threading.Lock()
        self._client = client or anthropic.Anthropic(
            api_key=settings.anthropic_api_key, timeout=self.cfg.timeout_s, max_retries=self.cfg.max_retries
        )

    def model_for(self, role: ModelRole) -> str:
        model = self.models[role]
        if not model:
            raise LLMError(f"{'LLM_MODEL' if role == 'strong' else 'LLM_MODEL_FAST'} is not set in .env")
        return model

    def structured(self, prompt: str, schema: type[T], *, system: str, role: ModelRole, agent: str) -> T:
        """Ask for output matching `schema` (structured outputs guarantee valid JSON for it).

        Transport problems (timeouts, 429, 5xx) are retried inside the SDK. If the reply still cannot
        be used (refusal, cut off at max_tokens, fails validation) the call is repeated once with the
        reason appended; a second failure raises LLMError.
        """
        model = self.model_for(role)
        effort = self.cfg.effort if role == "strong" else self.cfg.effort_fast
        content = prompt
        problem = ""
        for _ in range(2):
            start = time.perf_counter()
            try:
                response = self._client.messages.parse(
                    model=model,
                    max_tokens=self.cfg.max_tokens,
                    system=system,
                    messages=[{"role": "user", "content": content}],
                    output_format=schema,
                    output_config={"effort": effort},
                )
            except (ValidationError, ValueError) as exc:
                problem = f"output did not match the schema: {str(exc)[:300]}"
                self._record(agent, model, None, start, problem)
            except anthropic.APIError as exc:
                self._record(agent, model, None, start, f"{type(exc).__name__}: {exc}")
                raise LLMError(f"{type(exc).__name__}: {exc}") from exc
            else:
                self._record(agent, model, response.usage, start, None)
                if response.stop_reason == "refusal":
                    raise LLMError("the model declined to answer")
                if response.parsed_output is not None:
                    return response.parsed_output
                problem = f"no usable output (stop_reason={response.stop_reason})"
            content = (
                f"{prompt}\n\nYour previous reply could not be used ({problem}). Reply again, following the schema."
            )
        raise LLMError(problem)

    def _record(
        self, agent: str, model: str, usage: anthropic.types.Usage | None, start: float, error: str | None
    ) -> None:
        ms = round((time.perf_counter() - start) * 1000)
        in_tok = usage.input_tokens if usage else 0
        out_tok = usage.output_tokens if usage else 0
        price_in, price_out = self.cfg.prices_per_mtok.get(model, (0.0, 0.0))
        with self._lock:
            self.usage.calls += 1
            self.usage.failures += error is not None
            self.usage.input_tokens += in_tok
            self.usage.output_tokens += out_tok
            self.usage.cost_usd += (in_tok * price_in + out_tok * price_out) / 1_000_000
        self.trace.log(
            agent,
            "llm_call",
            llm={"model": model, "in_tokens": in_tok, "out_tokens": out_tok, "ms": ms},
            status="error" if error else "ok",
            error=error,
        )
