from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache


class Settings(BaseSettings):
    # Groq
    groq_api_key: str
    groq_model: str = "llama-3.3-70b-versatile"

    # Storage
    upload_dir: str = "uploads"
    database_url: str = "./clinical_fhir.db"

    # App
    app_env: str = "development"
    log_level: str = "INFO"

    # OCR: minimum extractable character count to treat a PDF as "text-based"
    # PDFs with fewer chars than this threshold are treated as scanned images
    pdf_text_threshold: int = 100

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


@lru_cache
def get_settings() -> Settings:
    return Settings()
