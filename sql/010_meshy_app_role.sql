-- Create the non-superuser `meshy_app` role
-- used in production so skill_calls RLS actually applies (spec §5.3).
-- Idempotent so re-running migrations in dev doesn't error.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'meshy_app') THEN
        CREATE ROLE meshy_app LOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
END $$;

GRANT USAGE ON SCHEMA public TO meshy_app;

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO meshy_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO meshy_app;

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO meshy_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO meshy_app;

GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO meshy_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT EXECUTE ON FUNCTIONS TO meshy_app;

GRANT meshy_app TO CURRENT_USER;

COMMENT ON ROLE meshy_app IS
    'Non-superuser application role. Production meshy-server connects as this role (DATABASE_URL=...meshy_app...) OR as the bootstrap user with MESHY_SET_ROLE_TO=meshy_app. Either way skill_calls RLS applies because NOBYPASSRLS is set.';
