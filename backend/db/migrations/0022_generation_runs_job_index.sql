-- Retried generation jobs look up their completed runs to skip already saved plan slots.
CREATE INDEX IF NOT EXISTS ix_generation_runs_job_status ON generation_runs (generation_job_id, status);
