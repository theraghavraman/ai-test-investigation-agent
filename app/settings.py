"""Environment-only operational configuration; never expose secrets in reports."""
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    api_key: SecretStr = Field(min_length=32)
    openai_api_key: SecretStr = SecretStr("")
    openai_model: str = "gpt-5.4-mini"
    # Optional OpenAI-compatible endpoint that implements /v1/responses (LM Studio, Ollama).
    # When set, no OPENAI_API_KEY is needed and approved code is sent to this URL instead of OpenAI.
    llm_base_url: str = ""
    # Sampling temperature, sent only to a local endpoint; low values keep small models calling tools.
    llm_temperature: float = Field(default=0.1, ge=0, le=2)
    database_url: str = "postgresql+psycopg://agent:agent@localhost:5432/agent"
    redis_url: str = "redis://localhost:6379/0"
    data_dir: Path = Path("/srv/agent-data")
    runner_image: str = "ai-test-runner:local"
    docker_binary: str = "docker"
    # Serve /docs and /openapi.json. Keep off anywhere the API is reachable by others.
    enable_docs: bool = False
    # Requests per client per minute on /v1/ routes, counted before authentication. 0 disables.
    rate_limit_per_minute: int = Field(default=60, ge=0, le=100_000)

    max_archive_bytes: int = Field(default=8_000_000, ge=1000, le=50_000_000)
    max_extracted_bytes: int = Field(default=1_000_000, ge=1000, le=10_000_000)
    max_file_bytes: int = Field(default=128_000, ge=1000, le=500_000)
    max_approved_files: int = Field(default=30, ge=1, le=100)
    max_queued_per_project: int = Field(default=20, ge=1, le=1000)
    max_tool_rounds: int = Field(default=12, ge=1, le=50)
    max_tool_calls: int = Field(default=32, ge=1, le=150)
    max_generated_tests: int = Field(default=8, ge=1, le=100)
    max_test_runs: int = Field(default=4, ge=1, le=20)
    max_test_code_bytes: int = Field(default=16_000, ge=1000, le=100_000)
    investigation_timeout_seconds: int = Field(default=300, ge=60, le=1200)
    # Upper bound allows slow local models; keep well under investigation_timeout_seconds.
    openai_call_timeout_seconds: int = Field(default=60, ge=5, le=900)
    max_model_output_tokens: int = Field(default=8192, ge=512, le=16000)
    test_timeout_seconds: int = Field(default=45, ge=5, le=180)
    docker_memory_mb: int = Field(default=256, ge=128, le=2048)
    docker_cpus: float = Field(default=1.0, ge=0.25, le=4)
    docker_pids_limit: int = Field(default=64, ge=16, le=256)
    docker_log_bytes: int = Field(default=40_000, ge=1000, le=200_000)
    report_retention_days: int = Field(default=30, ge=1, le=365)

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value: SecretStr) -> SecretStr:
        if value.get_secret_value().startswith("REPLACE_"):
            raise ValueError("replace the insecure placeholder API_KEY before starting")
        return value

    @field_validator("llm_base_url")
    @classmethod
    def validate_llm_base_url(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return ""
        from urllib.parse import urlparse

        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("LLM_BASE_URL must be an http(s) URL such as http://host.docker.internal:1234/v1")
        return value.rstrip("/")

    @field_validator("runner_image")
    @classmethod
    def validate_image(cls, value: str) -> str:
        # Configuration is admin-controlled; forbid accidental shell-like values.
        import re

        if not re.fullmatch(r"[a-zA-Z0-9._/@:-]{3,240}", value):
            raise ValueError("runner_image must be a Docker image reference")
        return value


@lru_cache
def get_settings() -> Settings:  # type: ignore[unindent]
    return Settings()
