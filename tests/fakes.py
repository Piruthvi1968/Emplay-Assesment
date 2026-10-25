"""Test doubles for the LLM and the search models: no network, no model downloads, deterministic."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from app.llm_client import LLMError
from app.search.bm25_index import tokenize


class FakeLLM:
    """Implements the `LLM` protocol with canned replies per schema name.

    A reply can be a model instance, an LLMError (raised), or a callable(prompt) -> model.
    """

    def __init__(self, replies: dict[str, Any] | None = None) -> None:
        self.replies = replies or {}
        self.calls: list[dict[str, Any]] = []

    def structured(self, prompt: str, schema: type[BaseModel], *, system: str, role: str, agent: str) -> BaseModel:
        self.calls.append({"schema": schema.__name__, "prompt": prompt, "role": role, "agent": agent})
        reply = self.replies.get(schema.__name__)
        if reply is None:
            raise LLMError(f"FakeLLM has no reply for {schema.__name__}")
        if isinstance(reply, LLMError):
            raise reply
        return reply(prompt) if isinstance(reply, Callable) else reply


DIM = 64


class FakeEmbedder:
    """Hashed bag-of-words vectors: deterministic, and similar texts get similar vectors."""

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * DIM
        for tok in tokenize(text):
            v[int(hashlib.md5(tok.encode()).hexdigest(), 16) % DIM] += 1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)


class FakeReranker:
    """Score = number of query tokens found in the text."""

    def score(self, query: str, texts: list[str]) -> list[float]:
        q = set(tokenize(query))
        return [float(len(q & set(tokenize(t)))) - 0.5 for t in texts]
