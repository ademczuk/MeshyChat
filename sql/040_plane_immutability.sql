-- 040_plane_immutability.sql
--
-- Dual-mode tenancy hardening (deny-matrix root cause RC1). The keystone
-- meshy_same_tenant_or_devgroup has a `caller = target` branch that is correct
-- and load-bearing for legitimate same-owner reads, so it is intentionally NOT
-- changed. What made it exploitable was the MUTABILITY of dashboard_users.plane
-- and trust_group_id: a dev owner could UPDATE plane to 'product' (DG1 enabler)
-- or a product owner could UPDATE plane to 'dev' and join a group (RM3), and a
-- dev owner could sit in the legal-but-malformed plane='dev' + NULL group state
-- (IG5). This migration removes that mutability and enforces the invariant.
--
-- plane is set at INSERT (the onboarding flow defaults new owners to 'product';
-- dev owners are provisioned dev + group at creation) and is never changed in
-- production. There is no legitimate post-insert plane or group change. A
-- controlled promotion path, if ever required, must be a separate audited
-- SECURITY DEFINER function, never a raw UPDATE.

-- Invariant (closes IG5): an owner is dev IFF it carries a trust group.
-- Verified zero violations on the live DB before adding
-- (dev_no_group = 0, product_with_group = 0 across all 290 rows).
ALTER TABLE dashboard_users
    ADD CONSTRAINT dashboard_users_plane_group_ck
    CHECK ((plane = 'dev') = (trust_group_id IS NOT NULL));

-- Immutability (closes the DG1 enabler + RM3): plane and trust_group_id cannot
-- change once the row exists. Other column updates (tier, onboarding_dismissed_at,
-- authentik_sub) are unaffected because they leave plane/trust_group_id equal.
CREATE OR REPLACE FUNCTION dashboard_users_freeze_plane_group()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.plane IS DISTINCT FROM OLD.plane THEN
        RAISE EXCEPTION
            'dashboard_users.plane is immutable (was %, attempted %)',
            OLD.plane, NEW.plane
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.trust_group_id IS DISTINCT FROM OLD.trust_group_id THEN
        RAISE EXCEPTION
            'dashboard_users.trust_group_id is immutable (was %, attempted %)',
            OLD.trust_group_id, NEW.trust_group_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER dashboard_users_freeze_plane_group_trg
    BEFORE UPDATE ON dashboard_users
    FOR EACH ROW
    EXECUTE FUNCTION dashboard_users_freeze_plane_group();
