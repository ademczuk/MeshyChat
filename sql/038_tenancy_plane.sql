-- Migration 038: dual-mode tenancy spine (schema half).
ALTER TABLE dashboard_users
  ADD COLUMN IF NOT EXISTS plane TEXT NOT NULL DEFAULT 'product'
    CHECK (plane IN ('product','dev')),
  ADD COLUMN IF NOT EXISTS trust_group_id UUID;

UPDATE dashboard_users
  SET plane = 'dev',
      trust_group_id = 'dddddddd-dddd-dddd-dddd-dddddddddddd'
  WHERE id IN (
    '11111111-1111-1111-1111-111111111111',
    '382b129e-9d81-4280-992a-b1dce999c7c5',
    'a40c72b8-9050-4025-b9a2-c34b7d5617a5'
  );

ALTER TABLE shared_memory_namespace
  ADD COLUMN IF NOT EXISTS claimant_plane TEXT NOT NULL DEFAULT 'dev';

CREATE OR REPLACE FUNCTION enforce_ns_dev_only() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE owner_plane TEXT;
BEGIN
    SELECT plane INTO owner_plane FROM dashboard_users WHERE id = NEW.owner_owner_id;
    NEW.claimant_plane := COALESCE(owner_plane, 'product');
    IF NEW.claimant_plane IS DISTINCT FROM 'dev' THEN
        RAISE EXCEPTION
          'shared_memory claim denied: owner % is plane=% (product tenants cannot claim shared/*)',
          NEW.owner_owner_id, NEW.claimant_plane;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS ns_dev_only_trigger ON shared_memory_namespace;
CREATE TRIGGER ns_dev_only_trigger
  BEFORE INSERT OR UPDATE ON shared_memory_namespace
  FOR EACH ROW EXECUTE FUNCTION enforce_ns_dev_only();
