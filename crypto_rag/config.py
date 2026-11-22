"""Application settings, read from the environment and `.env`.

Every tunable that affects results (model names, decay half-life, thresholds)
lives here so that an experiment can be reproduced from its saved config.
"""

import contextlib
import json
import logging
import re
from datetime import UTC, datetime
from functools import lru_cache
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Database -----------------------------------------------------------
    database_url: str = "postgresql+psycopg://crypto:change_me@localhost:5433/crypto_rag"
    # Integration tests DROP and recreate tables; they only ever run against this URL.
    test_database_url: str = "postgresql+psycopg://crypto:change_me@localhost:5433/crypto_rag_test"

    # --- External APIs ------------------------------------------------------
    openai_api_key: SecretStr = SecretStr("")
    guardian_api_key: SecretStr = SecretStr("")

    # --- OpenAI models (verified against the account, see docs/decisions.md) --
    router_model: str = "gpt-5.6-luna"
    label_model: str = "gpt-5.6-luna"
    generator_model: str = "gpt-5.6-terra"
    judge_model: str = "gpt-5.6-sol"
    # None = parameter not sent. The gpt-5.6 models reject `temperature` (verified
    # 2026-11-02); set LLM_TEMPERATURE=0 only for models that accept it.
    llm_temperature: float | None = None
    embedding_model: str = "text-embedding-3-large"
    embedding_dimensions: int = 1536
    # USD per 1M tokens: [input, output], standard (uncached) API rates as of
    # 2026-11-02 (Luna/Terra prices cut on 2026-07-30); Sol as of 2026-11-08 (judge only).
    llm_prices: dict[str, list[float]] = Field(
        default_factory=lambda: {
            "text-embedding-3-large": [0.13, 0.0],
            "gpt-5.6-luna": [0.20, 1.20],
            "gpt-5.6-terra": [2.00, 12.00],
            "gpt-5.6-sol": [4.00, 20.00],
        }
    )

    # --- Data scope ---------------------------------------------------------
    symbols: list[str] = Field(default_factory=lambda: ["BTCUSDT", "ETHUSDT"])
    start_date: datetime = datetime(2020, 1, 1, tzinfo=UTC)
    # Sources whose articles feed the forecasting news features (see forecasting/features.py).
    feature_news_sources: list[str] = Field(default_factory=lambda: ["guardian"])
    # Topic/tone labels for Hugging Face dataset articles cost one model call each and are
    # not needed while news features come from The Guardian only. Off by default.
    label_hf_articles: bool = False
    embedding_batch_size: int = 256
    # Set in .env only after the author confirms them from `guardian tags` output
    # (stage 4). Empty means "not confirmed": Guardian jobs are skipped.
    # Comma-separated in .env (GUARDIAN_TAGS=a/b,c/d): a JSON list loses its quotes
    # when the file is sourced by a shell (`set -a; . .env`).
    guardian_tags: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # Paid OpenAI steps (embeddings, labels) in the scheduled collector. Off by
    # default so a restart never starts bulk spending on its own.
    llm_processing_enabled: bool = False

    # --- Retrieval ----------------------------------------------------------
    decay_half_life_hours: float = 48.0
    rrf_k: int = 60
    retrieval_candidates: int = 50
    retrieval_top_k: int = 6
    max_chunks_per_article: int = 2

    # --- Forecasting --------------------------------------------------------
    buy_threshold_pct: float = 2.0
    commission_pct: float = 0.1
    slippage_pct: float = 0.05
    random_seed: int = 42

    # --- API ----------------------------------------------------------------
    # Vite dev server and the compose UI container (8090; 8080 is taken on the dev machine).
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://localhost:8090"]
    )

    log_level: str = "INFO"

    @field_validator("guardian_tags", mode="before")
    @classmethod
    def _split_tags(cls, value: object) -> object:
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("["):
                with contextlib.suppress(json.JSONDecodeError):
                    return json.loads(text)
                text = text.strip("[]")
            return [t.strip().strip("'\"") for t in text.split(",") if t.strip().strip("'\"")]
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()


# API keys travel as URL parameters (Guardian `api-key=`), and httpx puts the full
# URL into exception messages that retry and error logs print.
_SECRET_PARAM = re.compile(r"((?:api[-_]?key|apikey|token)=)[^&\s'\"]+", re.IGNORECASE)


class RedactingFormatter(logging.Formatter):
    """Masks secret URL parameters in the fully formatted record, tracebacks included."""

    def format(self, record: logging.LogRecord) -> str:
        return _SECRET_PARAM.sub(r"\1***", super().format(record))


def setup_logging(level: str | None = None) -> None:
    logging.basicConfig(level=level or get_settings().log_level)
    formatter = RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    for handler in logging.getLogger().handlers:
        handler.setFormatter(formatter)
    # One INFO line per HTTP request drowns the ingestion progress messages.
    logging.getLogger("httpx").setLevel(logging.WARNING)
