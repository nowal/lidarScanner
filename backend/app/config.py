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


settings = Settings()
