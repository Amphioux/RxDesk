from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    RXDESK_SECRET_KEY: str
    RXDESK_DEFAULT_ADMIN_PASSWORD: str
    
    MASSPRO_SQL_HOST: str = "."
    MASSPRO_SQL_PORT: int = 1433
    MASSPRO_SQL_USER: str = "MassproReader"
    MASSPRO_SQL_PASSWORD: str
    MASSPRO_SQL_DRIVER: str = "{ODBC Driver 17 for SQL Server}"
    MASSPRO_DB_PREFIX: str = "ST"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

settings = Settings()