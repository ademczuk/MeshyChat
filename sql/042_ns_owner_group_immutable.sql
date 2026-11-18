-- Migration 042: close DV1 - cross-trust-group namespace-ownership takeover.
--
-- The 038 claim wall (enforce_ns_dev_only) validates that a namespace owner is dev-plane,
-- but on UPDATE it did NOT check the trust group, so a dev in a DIFFERENT group could
-- seize another group's shared/* namespace by UPDATEing owner_owner_id (DV1). This amends
-- the trigger function so an owner CHANGE on UPDATE must stay within the prior owner's
-- trust group. The INSERT/claim path and the dev-plane assertion are unchanged. Pairs with
-- bxxxxx's 040 plane-immutability + 041; batched into one meshy-server rebuild.

CREATE OR REPLACE FUNCTION enforce_ns_dev_only() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    owner_plane TEXT;
    new_group   UUID;
    old_group   UUID;
BEGIN
    SELECT plane, trust_group_id INTO owner_plane, new_group
        FROM dashboard_users WHERE id = NEW.owner_owner_id;
    NEW.claimant_plane := COALESCE(owner_plane, 'product');
    IF NEW.claimant_plane IS DISTINCT FROM 'dev' THEN
        RAISE EXCEPTION
          'shared_memory claim denied: owner % is plane=% (product tenants cannot claim shared/*)',
          NEW.owner_owner_id, NEW.claimant_plane;
    END IF;

    -- DV1: an owner CHANGE on UPDATE must stay within the prior owner's trust group. A dev
    -- in a different group (or with a NULL group) cannot take over another group's claim.
    IF TG_OP = 'UPDATE' AND NEW.owner_owner_id IS DISTINCT FROM OLD.owner_owner_id THEN
        SELECT trust_group_id INTO old_group FROM dashboard_users WHERE id = OLD.owner_owner_id;
        IF new_group IS NULL OR new_group IS DISTINCT FROM old_group THEN
            RAISE EXCEPTION
              'shared_memory namespace owner change denied: new owner % (group %) is not in the prior owner''s trust group % (cross-group takeover)',
              NEW.owner_owner_id, new_group, old_group;
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

-- Re-bind the trigger to the amended function (BEFORE INSERT OR UPDATE, unchanged shape).
DROP TRIGGER IF EXISTS ns_dev_only_trigger ON shared_memory_namespace;
CREATE TRIGGER ns_dev_only_trigger
  BEFORE INSERT OR UPDATE ON shared_memory_namespace
  FOR EACH ROW EXECUTE FUNCTION enforce_ns_dev_only();

-- DOWN (for both-directions validation): restore the 038 function body (no group check).
--   CREATE OR REPLACE FUNCTION enforce_ns_dev_only() ... (038 version) ...; re-bind trigger.
