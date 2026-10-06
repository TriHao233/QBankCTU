-- Subjects a teacher teaches; SUBJECT-scoped sharing is limited to these subjects.
ALTER TABLE users ADD COLUMN teaching_subject_ids jsonb NOT NULL DEFAULT '[]'::jsonb;

CREATE INDEX ix_users_teaching_subject_ids ON users USING gin (teaching_subject_ids);
