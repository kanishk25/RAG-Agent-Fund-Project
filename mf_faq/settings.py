"""Runtime settings and config loading (P1.3).

Config splits in two on purpose:
  - `Settings`  — tunables, env-overridable (thresholds, model, budgets)
  - YAML files  — the corpus definition and link registry, which are reviewable
                  data rather than code, and are validated by mf_faq.schemas

`GROQ_API_KEY` is intentionally optional. Ingestion needs no model credentials
(ARCH §8.1), so the daily GitHub Actions workflow must be able to import this
module and run the pipeline without one. Generation calls check for it at the
point of use, not at import.
"""

from __future__ import annotations

import functools
import json
import pathlib
from typing import Annotated

import yaml
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from mf_faq.schemas import RefusalLinksConfig, SourcesConfig

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"
DATA_DIR = REPO_ROOT / "data"


class Settings(BaseSettings):
    """Env-overridable tunables. Prefix: MF_FAQ_ (except GROQ_API_KEY)."""

    model_config = SettingsConfigDict(
        env_prefix="MF_FAQ_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Generation (Phase 4) ---
    # Model choice is effectively forced: on Groq only the GPT-OSS models support
    # strict structured outputs, which the design depends on (ARCH §3).
    model: str = "openai/gpt-oss-120b"
    temperature: float = 0.0  # extraction, not writing
    max_output_tokens: int = 800

    # --- Retrieval (Phase 3) ---
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    top_k: int = 4
    similarity_floor: float = Field(default=0.35, ge=0.0, le=1.0)

    # --- Groq free-tier budget guardrails (ARCH §15.4) ---
    # TPM binds long before RPM: ~2,500-3,000 tokens/request against 8,000 TPM
    # is roughly 2-3 questions per minute, not 30. These are the numbers the
    # eval harness throttles against (eval.md §7).
    groq_tpm_limit: int = 8_000
    groq_rpm_limit: int = 30
    groq_daily_token_budget: int = 200_000
    groq_daily_request_budget: int = 1_000
    max_query_chars: int = 1_000  # reject oversized queries before spending tokens

    # --- Storage ---
    chroma_dir: pathlib.Path = DATA_DIR / "chroma"
    registry_db: pathlib.Path = DATA_DIR / "registry.db"
    collection_name: str = "mf_facts"

    # --- Ops ---
    log_level: str = "INFO"
    groq_api_key: str = Field(default="", alias="GROQ_API_KEY")

    # --- API (Phase 7) ---
    # The frontend (frontend/, a separate Next.js deployable per ARCH §5) calls
    # this API cross-origin from its own dev server / host. Defaults cover the
    # Next.js dev server; production origins are added via env, never widened
    # to "*" (P4.1's PII gate assumes requests come only from a trusted UI).
    #
    # `NoDecode` opts this field out of pydantic-settings' default behaviour of
    # `json.loads`-ing a list-typed env var before validation ever runs — that
    # default made a plain URL (`https://foo.vercel.app`, no brackets, the
    # obvious thing to paste into a platform's env var UI) crash the app at
    # startup with a raw `JSONDecodeError` traceback instead of a readable
    # config error. The validator below accepts a JSON array (still valid,
    # still documented in deployment.md) OR a comma-separated string OR a
    # single bare origin.
    cors_allow_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "http://localhost:3000",
            "http://127.0.0.1:3000",
            # `next dev` falls back to the next free port when 3000 is taken.
            "http://localhost:3001",
            "http://127.0.0.1:3001",
        ]
    )

    # Optional, in addition to the exact-match list above. Vercel mints a new
    # random-hash preview URL on every deploy (`project-<hash>-<user>.vercel.app`),
    # so listing them individually doesn't scale — a regex matching the whole
    # project's preview pattern does. `None` disables this path entirely (the
    # exact-match list above still applies either way).
    cors_allow_origin_regex: str | None = None

    @field_validator("cors_allow_origins", mode="before")
    @classmethod
    def _parse_cors_allow_origins(cls, value: object) -> object:
        if not isinstance(value, str):
            return value  # already a list (e.g. the default) — leave as-is
        stripped = value.strip()
        if not stripped:
            return []
        if stripped.startswith("["):
            try:
                return json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"MF_FAQ_CORS_ALLOW_ORIGINS looks like a JSON array but isn't valid "
                    f"JSON: {stripped!r}"
                ) from exc
        return [origin.strip() for origin in stripped.split(",") if origin.strip()]

    def require_groq_key(self) -> str:
        """Fetch the API key at point of use, not at import.

        Ingestion must run without one; only generation needs it.
        """
        if not self.groq_api_key:
            raise RuntimeError(
                "GROQ_API_KEY is not set. Copy .env.example to .env and fill it in. "
                "(Ingestion does not need this — only query-time generation does.)"
            )
        return self.groq_api_key


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def _load_yaml(path: pathlib.Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"config file missing: {path}")
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path} did not parse to a mapping")
    return data


@functools.lru_cache(maxsize=1)
def get_sources(path: pathlib.Path | None = None) -> SourcesConfig:
    """Load and validate the corpus definition. Raises loudly if malformed."""
    return SourcesConfig.model_validate(_load_yaml(path or CONFIG_DIR / "sources.yaml"))


@functools.lru_cache(maxsize=1)
def get_refusal_links(path: pathlib.Path | None = None) -> RefusalLinksConfig:
    """Load and validate the refusal link registry."""
    return RefusalLinksConfig.model_validate(_load_yaml(path or CONFIG_DIR / "refusal_links.yaml"))


def validate_all_configs() -> None:
    """Validate every config. Called at app startup so failures surface at boot."""
    get_sources()
    get_refusal_links()
