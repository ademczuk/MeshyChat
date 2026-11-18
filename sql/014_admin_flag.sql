-- Admin flag on dashboard_users.
-- Gates the new /v1/admin/* routes. Default FALSE so existing users
-- have no admin privileges; Lxxxx's row is updated via direct SQL
-- in dev + production as part of admin setup.

ALTER TABLE dashboard_users
    ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT FALSE;
