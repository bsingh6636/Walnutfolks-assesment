"""Runtime configuration.

Each attribute below is read from an environment variable with the same name
(case does not matter), or from the local .env file during development.
Example: `db_host` comes from DB_HOST.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # MySQL connection. Kept as separate fields so passwords with special
    # characters need no URL-encoding.
    db_host: str
    db_port: int = 3306
    db_user: str
    db_password: str = ""
    db_name: str

    # Hosted MySQL providers require an encrypted connection.
    # Set to false for a local MySQL without certificates.
    db_ssl: bool = True

    # The assessment requires a 30 second simulated delay per transaction.
    # Lower it locally to iterate faster.
    processing_delay_seconds: float = 30.0


# One settings object, created when the app starts, imported everywhere else.
settings = Settings()
