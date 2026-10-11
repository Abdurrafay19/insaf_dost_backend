from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    groq_api_key: str = ""
    qdrant_url: str = ""
    qdrant_api_key: str = ""
    environment: str = "production"
    # Comma-separated list of browser origins allowed to call this API.
    allowed_origins: str = (
        "https://insafdostai.abdurrafaylabs.tech,"
        "https://insafdostai.vercel.app,"
        "http://localhost:3000,"
        "http://localhost:5173"
    )

    model_config = SettingsConfigDict(
        env_file=".env", 
        env_file_encoding="utf-8", 
        extra="ignore"
    )

@lru_cache()
def get_settings() -> Settings:
    return Settings()