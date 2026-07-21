-- 02_meta.sql
-- One row per load attempt. Every bronze row carries its _batch_id back to here,
-- so you can trace any record to the file + run that produced it, and reload a
-- single month cleanly (delete where _batch_id in (...)) when BTS revises history.

CREATE TABLE IF NOT EXISTS meta.ingest_batch (
    batch_id        BIGSERIAL PRIMARY KEY,
    target_table    TEXT        NOT NULL,                 -- e.g. 'bronze.t100_segment'
    source_file     TEXT,                                 -- original filename / path
    data_scope      TEXT        CHECK (data_scope IN ('domestic','international') OR data_scope IS NULL),
    reporting_year  INT,                                  -- period the file covers
    reporting_month INT         CHECK (reporting_month BETWEEN 1 AND 12 OR reporting_month IS NULL),
    row_count       BIGINT,
    status          TEXT        NOT NULL DEFAULT 'started'
                                CHECK (status IN ('started','loaded','failed')),
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    error_message   TEXT,
    notes           TEXT
);

CREATE INDEX IF NOT EXISTS ix_ingest_batch_table_period
    ON meta.ingest_batch (target_table, reporting_year, reporting_month);
