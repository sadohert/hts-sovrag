"""Runtime configuration. Everything comes from env; see .env.example."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    llm_base_url: str = "http://localhost:12434/engines/v1"
    llm_model: str = "ai/qwen3.5:9b-q4_K_M"
    llm_api_key: str = "not-needed"

    embed_base_url: str = "http://localhost:12434/engines/v1"
    embed_model: str = "hf.co/gpustack/bge-m3-gguf:Q8_0"
    embed_dim: int = 1024
    embed_api_key: str = "not-needed"

    judge_base_url: str = ""
    judge_model: str = ""
    judge_api_key: str = ""

    database_url: str = "postgresql://sovrag:sovrag@localhost:5432/sovrag"
    audit_log_path: str = "./audit.jsonl"

    # MCP server only. Bound at startup; never read from tool arguments.
    principal: str = ""


def get_settings() -> Settings:
    return Settings()
