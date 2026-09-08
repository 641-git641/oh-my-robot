from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )
    github_api_base_url: AnyHttpUrl = Field("https://api.github.com", alias="GITHUB_API_BASE_URL")
    github_token: SecretStr | None = Field(None, alias="GITHUB_TOKEN")
    github_webhook_secret: SecretStr | None = Field(None, alias="GITHUB_WEBHOOK_SECRET")
    github_repo_allowlist_raw: str = Field("", alias="GITHUB_REPO_ALLOWLIST")

    deepseek_api_base_url: AnyHttpUrl = Field("https://api.deepseek.com", alias="DEEPSEEK_API_BASE_URL")
    deepseek_api_key: SecretStr | None = Field(None, alias="DEEPSEEK_API_KEY")
    deepseek_model: str = Field("deepseek-chat", alias="DEEPSEEK_MODEL")
    deepseek_thinking_enabled: bool = Field(False, alias="DEEPSEEK_THINKING_ENABLED")
    deepseek_reasoning_effort: Literal["low", "high", "max"] = Field("high", alias="DEEPSEEK_REASONING_EFFORT")

    bind_host: str = Field("0.0.0.0", alias="ROBOT_BIND_HOST")
    bind_port: int = Field(8090, alias="ROBOT_BIND_PORT", gt=0, le=65535)
    database_path: Path = Field(Path("data/review-bot.sqlite3"), alias="ROBOT_DATABASE_PATH")
    review_rule_file: Path = Field(Path("review-rules.md"), alias="ROBOT_REVIEW_RULE_FILE")
    review_path_rule_file: Path = Field(Path("review-rules.toml"), alias="ROBOT_REVIEW_PATH_RULE_FILE")
    max_diff_bytes: int = Field(200_000, alias="ROBOT_MAX_DIFF_BYTES", gt=0)
    max_review_bytes: int = Field(50_000, alias="ROBOT_MAX_REVIEW_BYTES", gt=0)
    request_timeout_seconds: float = Field(90.0, alias="ROBOT_REQUEST_TIMEOUT_SECONDS", gt=0)
    max_retries: int = Field(2, alias="ROBOT_MAX_RETRIES", ge=0, le=5)
    max_concurrency: int = Field(4, alias="ROBOT_MAX_CONCURRENCY", ge=1, le=32)
    shutdown_drain_seconds: float = Field(25.0, alias="ROBOT_SHUTDOWN_DRAIN_SECONDS", ge=0, le=300)
    review_enabled: bool = Field(True, alias="ROBOT_REVIEW_ENABLED")
    admin_token: SecretStr | None = Field(None, alias="ROBOT_ADMIN_TOKEN")
    review_mode: Literal["fast", "deep"] = Field("fast", alias="ROBOT_REVIEW_MODE")
    omp_command: str = Field("omp", alias="ROBOT_OMP_COMMAND")
    omp_model: str | None = Field(None, alias="ROBOT_OMP_MODEL")
    omp_sandboxed: bool = Field(False, alias="ROBOT_OMP_SANDBOXED")
    roboomp_webhook_url: AnyHttpUrl | None = Field(None, alias="ROBOT_ROBOOMP_WEBHOOK_URL")
    roboomp_timeout_seconds: float = Field(15.0, alias="ROBOT_ROBOOMP_TIMEOUT_SECONDS", gt=0, le=120)

    @property
    def repo_allowlist(self) -> frozenset[str]:
        return frozenset(item.strip().lower() for item in self.github_repo_allowlist_raw.split(",") if item.strip())

    @property
    def api_base_url(self) -> str:
        return str(self.github_api_base_url).rstrip("/")

    @property
    def deepseek_base_url(self) -> str:
        return str(self.deepseek_api_base_url).rstrip("/")

    def validate_runtime(self) -> None:
        missing: list[str] = []
        if self.github_token is None or not self.github_token.get_secret_value().strip():
            missing.append("GITHUB_TOKEN")
        if self.github_webhook_secret is None or not self.github_webhook_secret.get_secret_value().strip():
            missing.append("GITHUB_WEBHOOK_SECRET")
        if self.review_mode == "fast" and (
            self.deepseek_api_key is None or not self.deepseek_api_key.get_secret_value().strip()
        ):
            missing.append("DEEPSEEK_API_KEY")
        if self.review_mode == "deep" and not self.omp_command.strip():
            missing.append("ROBOT_OMP_COMMAND")
        if not self.repo_allowlist:
            missing.append("GITHUB_REPO_ALLOWLIST")
        if self.review_mode == "deep" and not self.omp_sandboxed:
            missing.append("ROBOT_OMP_SANDBOXED=true")
        if missing:
            raise ValueError(f"missing required robot configuration: {', '.join(missing)}")

    @field_validator("deepseek_model", mode="before")
    @classmethod
    def reject_blank_text(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            raise ValueError("configuration value must not be blank")
        return value

    @field_validator("roboomp_webhook_url", mode="after")
    @classmethod
    def validate_roboomp_webhook_url(cls, value: AnyHttpUrl | None) -> AnyHttpUrl | None:
        if value is not None and value.scheme not in {"http", "https"}:
            raise ValueError("ROBOT_ROBOOMP_WEBHOOK_URL must use http or https")
        if value is not None and (value.username is not None or value.password is not None):
            raise ValueError("ROBOT_ROBOOMP_WEBHOOK_URL must not contain credentials")
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.validate_runtime()
    return settings
