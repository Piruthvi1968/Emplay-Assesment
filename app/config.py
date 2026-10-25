"""Typed settings: config.yaml + fields.yaml + synonyms.yaml + environment (.env).

Everything tunable lives in the YAML files; secrets and model names come from the environment.
Use `get_settings()` everywhere; it loads once and caches the result.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parent.parent

FieldType = Literal["text", "datetime", "list", "contact", "summary"]


class PathsCfg(BaseModel):
    data_dir: Path
    index_dir: Path
    manifest: Path
    outputs_dir: Path
    traces_dir: Path


class IngestionCfg(BaseModel):
    chunk_target_tokens: int = 400
    chunk_max_tokens: int = 600
    chunk_overlap_tokens: int = 60
    min_chars_per_page: int = 30
    header_footer_min_repeat: float = 0.5


class EmbeddingCfg(BaseModel):
    model: str = "BAAI/bge-small-en-v1.5"
    batch_size: int = 32


class SearchCfg(BaseModel):
    default_mode: Literal["vector", "hybrid", "hybrid_rerank"] = "hybrid_rerank"
    candidates_per_list: int = 20
    rrf_k: int = 60
    rerank_candidates: int = 20
    rerank_model: str = "Xenova/ms-marco-MiniLM-L-6-v2"
    rerank_fusion: bool = False
    top_k_single: int = 5
    top_k_multi: int = 3
    addendum_k_single: int = 2
    addendum_k_multi: int = 1
    max_sub_queries: int = 4
    not_found_threshold: float = -12.0
    enable_pinning: bool = True


Effort = Literal["low", "medium", "high", "xhigh", "max"]


class LLMCfg(BaseModel):
    timeout_s: float = 120
    max_retries: int = 2
    max_tokens: int = 16000
    effort: Effort = "medium"
    effort_fast: Effort = "low"
    prices_per_mtok: dict[str, tuple[float, float]] = Field(default_factory=dict)


class AgentsCfg(BaseModel):
    max_validation_retries: int = 2
    extraction_parallel_groups: bool = True
    extraction_top_k: int = 6
    extraction_top_k_retry: int = 10
    extraction_max_per_file: int = 4
    opening_chunks_per_document: int = 2
    reconcile_batch_chunks: int = 20
    qa_follow_up_rounds: int = 1


class FieldSpec(BaseModel):
    """One extraction field from fields.yaml."""

    name: str
    group: str
    type: FieldType
    question: str
    hints: list[str] = Field(default_factory=list)
    format: str | None = None
    judge: bool = False  # free-text field: the validator also asks an LLM judge whether the quotes support it
    exclude_doc_types: list[str] = Field(default_factory=list)  # e.g. blank forms that crowd out the real answer


class Settings(BaseModel):
    paths: PathsCfg
    ingestion: IngestionCfg = Field(default_factory=IngestionCfg)
    embedding: EmbeddingCfg = Field(default_factory=EmbeddingCfg)
    search: SearchCfg = Field(default_factory=SearchCfg)
    llm: LLMCfg = Field(default_factory=LLMCfg)
    agents: AgentsCfg = Field(default_factory=AgentsCfg)

    groups: dict[str, str] = Field(default_factory=dict)
    fields: list[FieldSpec] = Field(default_factory=list)
    synonyms: dict[str, list[str]] = Field(default_factory=dict)

    # From the environment. Optional at load time: ingestion and search work without an API key.
    anthropic_api_key: str | None = None
    llm_model: str | None = None
    llm_model_fast: str | None = None

    def fields_by_group(self) -> dict[str, list[FieldSpec]]:
        """Field specs grouped by their `group`, keeping fields.yaml order."""
        out: dict[str, list[FieldSpec]] = {g: [] for g in self.groups}
        for f in self.fields:
            out.setdefault(f.group, []).append(f)
        return out


def _read_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _resolve(p: Path, root: Path) -> Path:
    return p if p.is_absolute() else root / p


def load_settings(root: Path = PROJECT_ROOT) -> Settings:
    """Build Settings from the YAML files under `root` and the environment."""
    load_dotenv(root / ".env")
    cfg = _read_yaml(root / "config.yaml")
    fields_doc = _read_yaml(root / "fields.yaml")
    synonyms = _read_yaml(root / "synonyms.yaml")

    settings = Settings(
        **cfg,
        groups=fields_doc.get("groups", {}),
        fields=fields_doc.get("fields", []),
        synonyms=synonyms,
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY") or None,
        llm_model=os.getenv("LLM_MODEL") or None,
        llm_model_fast=os.getenv("LLM_MODEL_FAST") or None,
    )
    for name in PathsCfg.model_fields:
        setattr(settings.paths, name, _resolve(getattr(settings.paths, name), root))

    unknown = {f.group for f in settings.fields} - set(settings.groups)
    if unknown:
        raise ValueError(f"fields.yaml uses undefined groups: {sorted(unknown)}")
    return settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide cached settings."""
    return load_settings()
