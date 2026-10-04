-- CLO classification aligned with CTĐT (kiến thức / kỹ năng / thái độ)
-- and mandatory rationale when creating a new evaluation policy version.

ALTER TABLE learning_outcomes
    ADD COLUMN IF NOT EXISTS outcome_type text NOT NULL DEFAULT 'kien_thuc';

ALTER TABLE learning_outcomes
    DROP CONSTRAINT IF EXISTS learning_outcomes_outcome_type_check;

ALTER TABLE learning_outcomes
    ADD CONSTRAINT learning_outcomes_outcome_type_check
    CHECK (outcome_type IN ('kien_thuc', 'ky_nang', 'thai_do'));

ALTER TABLE evaluation_policies
    ADD COLUMN IF NOT EXISTS change_reason text NOT NULL DEFAULT '';
