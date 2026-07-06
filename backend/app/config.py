from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="LIDARAI_", extra="ignore")

    app_name: str = "LidarAI Local Processor"
    api_prefix: str = "/api/v1"
    host: str = "0.0.0.0"
    port: int = 8000
    storage_dir: str = "./backend_storage"
    auth_token: str = ""
    cors_origins: str = "*"
    job_timeout_seconds: int = 1200
    default_processing_profile: str = "fast_onboarding"
    ai_provider: str = "openai"
    openai_api_key: str = ""
    openai_organization: str = ""
    openai_project: str = ""
    openai_model: str = "gpt-5.5"
    openai_fallback_model: str = ""
    openai_reasoning_effort: str = "medium"
    openai_request_timeout_seconds: int = 45
    openai_max_images_per_request: int = 1


settings = Settings()
