"""Runtime configuration. Everything comes from env; see .env.example."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    llm_base_url: str = "http://localhost:12434/engines/v1"
    llm_model: str = "ai/qwen3.5:9b-q4_K_M"
    llm_api_key: str = "not-needed"
    llm_timeout_s: float = 180.0
    # No transport-level retries by default: the client never silently re-sends a request.
    # answer() itself may make a second completion on the uncited-retry path (ADR 0008).
    llm_max_retries: int = 0

    embed_base_url: str = "http://localhost:12434/engines/v1"
    embed_model: str = "hf.co/gpustack/bge-m3-gguf:Q8_0"
    embed_dim: int = 1024
    embed_api_key: str = "not-needed"

    judge_base_url: str = ""
    judge_model: str = ""
    judge_api_key: str = ""

    database_url: str = "postgresql://sovrag:sovrag@localhost:5432/sovrag"
    audit_log_path: str = "./audit.jsonl"
    # Named principals for the CLI and the MCP server's static mode (ADR 0012).
    principals_path: str = "data/principals.yaml"

    # MCP server. MCP_IDENTITY is "static" or "mattermost"; required, no default (ADR 0009).
    # In static mode PRINCIPAL is resolved from PRINCIPALS_PATH at startup. In mattermost mode
    # every request must carry the shared secret as a bearer. Neither value is ever read from a
    # tool argument. MCP_ALLOWED_HOSTS lists the Host header values the server accepts, comma
    # separated, "mcp:*" for the Compose service name; empty keeps the SDK's localhost default.
    mcp_identity: str = ""
    principal: str = ""
    mcp_shared_secret: str = ""
    mcp_allowed_hosts: str = ""
    # MCP server, mattermost mode only: where the resolver reads user attributes, and the
    # bot token it reads them with. The token is a credential; it is never logged.
    mattermost_url: str = ""
    mattermost_bot_token: str = ""


def get_settings() -> Settings:
    return Settings()
