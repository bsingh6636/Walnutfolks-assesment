-- Applied at every startup. IF NOT EXISTS makes it safe to run repeatedly.
-- All DATETIME values are UTC: db.py sets the session time zone to +00:00,
-- so NOW() and CURRENT_TIMESTAMP produce UTC too.

CREATE TABLE IF NOT EXISTS transactions (
    id                  BIGINT        NOT NULL AUTO_INCREMENT,

    -- fields from the webhook payload
    transaction_id      VARCHAR(64)   NOT NULL,
    source_account      VARCHAR(64)   NOT NULL,
    destination_account VARCHAR(64)   NOT NULL,
    amount              DECIMAL(18,2) NOT NULL,   -- exact, never FLOAT for money
    currency            CHAR(3)       NOT NULL,

    -- processing state, used by the background worker
    status              ENUM('PROCESSING','PROCESSED','FAILED') NOT NULL DEFAULT 'PROCESSING',
    attempts            INT           NOT NULL DEFAULT 0,
    next_attempt_at     DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,  -- earliest time a worker may pick it up
    claimed_at          DATETIME      NULL,                                -- set while a worker holds the row
    last_error          TEXT          NULL,

    -- timing information required by the spec
    created_at          DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    processed_at        DATETIME      NULL,

    PRIMARY KEY (id),
    -- The idempotency guarantee: MySQL refuses a second row with the same transaction_id.
    UNIQUE KEY uq_transactions_transaction_id (transaction_id),
    -- The worker's poll query filters on exactly these two columns.
    INDEX ix_transactions_queue (status, next_attempt_at)
);
