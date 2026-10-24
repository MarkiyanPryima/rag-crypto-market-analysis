"""Application settings, read from the environment and `.env`.

Every tunable that affects results (model names, decay half-life, thresholds)
lives here so that an experiment can be reproduced from its saved config.
"""

import logging
from datetime import UTC, datetime
from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    # Minimal temperature; set to empty in .env for models that reject the parameter.
    llm_temperature: float | None = 0.0
    embedding_model: str = "text-embedding-3-large"
    embedding_dimensions: int = 1536
    # USD per 1M tokens: [input, output]. Fill in from the account's pricing page.
    llm_prices: dict[str, list[float]] = Field(
        default_factory=lambda: {"text-embedding-3-large": [0.13, 0.0]}
    )

    # --- Data scope ---------------------------------------------------------
    symbols: list[str] = Field(default_factory=lambda: ["BTCUSDT", "ETHUSDT"])
    start_date: datetime = datetime(2025, 1, 20, tzinfo=UTC)
    # PROVISIONAL until confirmed by the author from `guardian tags` output (stage 4).
    guardian_tags: list[str] = Field(
        default_factory=lambda: [
            "technology/bitcoin",
            "technology/cryptocurrencies",
            "technology/ethereum",
        ]
    )

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


@lru_cache
def get_settings() -> Settings:
    return Settings()


def setup_logging(level: str | None = None) -> None:
    logging.basicConfig(
        level=level or get_settings().log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # One INFO line per HTTP request drowns the ingestion progress messages.
    logging.getLogger("httpx").setLevel(logging.WARNING)
