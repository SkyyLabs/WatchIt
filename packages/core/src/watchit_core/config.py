from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field

class Settings(BaseSettings):
    # Database
    database_url: str | None = Field(default=None, alias="DATABASE_URL")

    # Policy
    policy_version: str = Field(default="1.0.0", alias="WATCHIT_POLICY_VERSION")

    # Schedule
    sched_name: str = Field(default="schoolnights", alias="WATCHIT_SCHEDULE_NAME")
    sched_days: str = Field(default="Mon,Tue,Wed,Thu", alias="WATCHIT_SCHEDULE_DAYS")
    sched_quiet: str = Field(default="21:00-07:00", alias="WATCHIT_SCHEDULE_QUIET")

    # Ollama
    llm_provider: str = Field(default="ollama", alias="WATCHIT_LLM_PROVIDER")
    ollama_model: str = Field(default="qwen2.5:7b-instruct-q4_K_M", alias="WATCHIT_OLLAMA_MODEL")
    ollama_base_url: str = Field(default="http://localhost:11434", alias="WATCHIT_OLLAMA_BASE_URL")
    cloud_llm_model: str = Field(default="gpt-4o-mini", alias="WATCHIT_CLOUD_LLM_MODEL")
    cloud_llm_base_url: str | None = Field(default=None, alias="WATCHIT_CLOUD_LLM_BASE_URL")
    cloud_llm_api_key: str | None = Field(default=None, alias="WATCHIT_CLOUD_LLM_API_KEY")
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    anthropic_model: str = Field(default="claude-3-5-sonnet-latest", alias="WATCHIT_ANTHROPIC_MODEL")

    # Clerk
    clerk_secret_key: str | None = Field(default=None, alias="CLERK_SECRET_KEY")
    clerk_jwks_url: str | None = Field(default=None, alias="CLERK_JWKS_URL")
    clerk_issuer: str | None = Field(default=None, alias="CLERK_ISSUER")

    # Server
    bind_host: str = Field(default="127.0.0.1", alias="WATCHIT_BIND_HOST")
    bind_port: int = Field(default=4849, alias="WATCHIT_BIND_PORT")
    # Comma-separated dashboard origins allowed by CORS (hosted deploys set this
    # to the production dashboard URL).
    cors_origins: str = Field(default="http://127.0.0.1:4848,http://localhost:4848", alias="WATCHIT_CORS_ORIGINS")

    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    # Features
    log_level: str = Field(default="INFO", alias="WATCHIT_LOG_LEVEL")
    agent_trace_files: bool = Field(default=False, alias="WATCHIT_AGENT_TRACE_FILES")
    processing_mode: str = Field(default="async", alias="WATCHIT_PROCESSING_MODE")
    embedded_agent_worker: bool = Field(default=True, alias="WATCHIT_EMBEDDED_AGENT_WORKER")
    agent_worker_poll_interval: float = Field(default=0.5, alias="WATCHIT_AGENT_WORKER_POLL_INTERVAL")
    # "memory": in-process fan-out only (single API instance, embedded worker).
    # "postgres": decisions travel via pg_notify so any API instance — and a
    # standalone worker — can reach every SSE subscriber. No new infra.
    sse_bus: str = Field(default="memory", alias="WATCHIT_SSE_BUS")
    # Hourly privacy sweep in the worker (docs/PRIVACY_LOGGING_AND_RETENTION.md).
    retention_sweep_enabled: bool = Field(default=True, alias="WATCHIT_RETENTION_SWEEP_ENABLED")
    url_decision_cache_enabled: bool = Field(default=True, alias="WATCHIT_URL_DECISION_CACHE_ENABLED")
    url_decision_cache_ttl_seconds: int = Field(default=86400, alias="WATCHIT_URL_DECISION_CACHE_TTL_SECONDS")
    url_decision_cache_min_confidence: float = Field(default=0.85, alias="WATCHIT_URL_DECISION_CACHE_MIN_CONFIDENCE")
    enable_ocr: bool = Field(default=True, alias="WATCHIT_ENABLE_OCR")
    ocr_confidence_threshold: float = Field(default=0.7, alias="WATCHIT_OCR_CONFIDENCE_THRESHOLD")
    save_screenshots: bool = Field(default=False, alias="WATCHIT_SAVE_SCREENSHOTS")
    screenshots_dir: str = Field(default="screenshots", alias="WATCHIT_SCREENSHOT_DIR")

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True, extra="ignore")

settings = Settings()
