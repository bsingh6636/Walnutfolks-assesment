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

    # Connection pool sizing & recycling (reconnect stale idle connections)
    db_pool_minsize: int = 5
    db_pool_maxsize: int = 30
    db_pool_recycle_seconds: int = 300

    # DEBUG includes idle worker pool acquisition timings; INFO logs real work.
    log_level: str = "INFO"

    # The assessment requires a 30 second simulated delay per transaction.
    # Lower it locally to iterate faster.
    processing_delay_seconds: float = 30.0

    # --- background worker ---
    # Run the worker inside the API process. Set to false when running it
    # separately with `python -m app.worker`.
    run_worker_in_api: bool = True
    # Most rows one claim may take. Every due row up to this many becomes one
    # batch: one shared 30 second wait, then one UPDATE for all of them.
    worker_batch_size: int = 1000
    # How long to wait before looking for work again when nothing was found.
    worker_poll_interval_seconds: float = 1.0
    # Give up and mark FAILED after this many attempts.
    worker_max_attempts: int = 3
    # Wait this long before retrying a failed attempt.
    worker_retry_delay_seconds: int = 30
    # A row claimed longer ago than this belongs to a worker that died; reclaim it.
    worker_stale_claim_seconds: int = 300


# One settings object, created when the app starts, imported everywhere else.
settings = Settings()
