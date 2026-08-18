from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "sqlite:///./tractorcloser.db"
    jwt_secret: str = "development-only-change-before-deployment"
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 480
    openai_api_key: str = ""
    openai_model: str = "gpt-5.6-luna"
    # Leave empty only while testing. In production this must contain the
    # comma-separated browser origins allowed to call the API.
    allowed_origins: str = ""
    integration_intake_enabled: bool = False
    app_environment: str = "testing"
    # Ideal credentials are deliberately supplied only through Render
    # environment variables. Never commit them to the repository.
    ideal_api_base_url: str = ""
    ideal_api_username: str = ""
    ideal_api_password: str = ""
    ideal_company_id: str = ""
    ideal_location_id: str = ""
    ideal_api_test_stock_number: str = ""
    # Web Push values are generated once for TractorCloser and kept in Render.
    # The public key is safe to return to the browser; the private key never is.
    vapid_public_key: str = ""
    vapid_private_key: str = ""
    vapid_claims_email: str = ""
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
