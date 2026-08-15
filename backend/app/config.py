from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "sqlite:///./tractorcloser.db"
    jwt_secret: str = "development-only-change-before-deployment"
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 480
    openai_api_key: str = ""
    openai_model: str = "gpt-5.6-luna"
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
