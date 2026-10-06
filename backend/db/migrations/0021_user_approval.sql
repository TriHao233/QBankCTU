-- Self-registered accounts wait for an administrator before they can use the system.
ALTER TABLE users
    ADD COLUMN approval_status text NOT NULL DEFAULT 'APPROVED'
    CHECK (approval_status IN ('PENDING', 'APPROVED'));

CREATE INDEX ix_users_pending_approval
    ON users(created_at DESC)
    WHERE approval_status = 'PENDING';
