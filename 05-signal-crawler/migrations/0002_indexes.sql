-- 0002_indexes.sql — additional indexes beyond UNIQUE-implied ones.

CREATE INDEX idx_jobs_status_source       ON jobs(status, source);
CREATE INDEX idx_jobs_run_after           ON jobs(run_after) WHERE run_after IS NOT NULL;
CREATE INDEX idx_inbox_pending_extract    ON inbox(extracted) WHERE extracted = 0;
CREATE INDEX idx_inbox_source_scraped_at  ON inbox(source, scraped_at);
CREATE INDEX idx_signals_app              ON signals(mentioned_app);
CREATE INDEX idx_signals_type             ON signals(signal_type);
