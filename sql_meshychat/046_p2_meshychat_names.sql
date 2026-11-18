-- P2 application-role and catalog-name cutover. Later migrations must name only
-- meshychat_app, meshychat_owner, and meshychat_* objects.
-- A fixed advisory key serializes per-cluster role provisioning across databases.
BEGIN;
SELECT pg_advisory_xact_lock(629245845241957046);

DO $p2_roles$
DECLARE
    old_role oid := to_regrole('meshy_app');
    new_role oid := to_regrole('meshychat_app');
    rec record;
    object_sql text;
    grant_suffix text;
    new_has boolean;
BEGIN
    IF new_role IS NULL THEN
        CREATE ROLE meshychat_app LOGIN NOSUPERUSER NOBYPASSRLS;
        new_role := to_regrole('meshychat_app');
    ELSIF (SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE oid = new_role) THEN
        RAISE EXCEPTION 'P2: meshychat_app must be NOSUPERUSER NOBYPASSRLS';
    END IF;

    IF NOT pg_has_role(current_user, new_role, 'member') THEN
        EXECUTE 'GRANT meshychat_app TO ' || quote_ident(current_user);
    END IF;

    -- In a fresh chain meshy_app exists. Copy then remove privileges so the final
    -- meshychat_app ACL is equal rather than a superset. A production role rename
    -- makes meshy_app absent, and its ACLs have already followed the rename.
    IF old_role IS NULL OR old_role = new_role THEN
        RETURN;
    END IF;

    FOR rec IN
        SELECT n.nspname,
               COALESCE(bool_or(a.grantee = old_role AND a.is_grantable), false) AS old_go,
               COALESCE(bool_or(a.grantee = new_role AND a.is_grantable), false) AS new_go,
               COALESCE(bool_or(a.grantee = old_role), false) AS old_has,
               COALESCE(bool_or(a.grantee = new_role), false) AS new_has
          FROM pg_namespace n
          LEFT JOIN LATERAL aclexplode(coalesce(n.nspacl, acldefault('n', n.nspowner))) a ON true
         WHERE n.nspname = 'public'
         GROUP BY n.nspname
    LOOP
        IF rec.old_has AND NOT rec.new_has THEN
            EXECUTE 'GRANT USAGE ON SCHEMA public TO meshychat_app' ||
                    CASE WHEN rec.old_go THEN ' WITH GRANT OPTION' ELSE '' END;
        ELSIF NOT rec.old_has AND rec.new_has THEN
            EXECUTE 'REVOKE ALL PRIVILEGES ON SCHEMA public FROM meshychat_app';
        ELSIF rec.old_has AND rec.new_has AND rec.old_go AND NOT rec.new_go THEN
            EXECUTE 'GRANT USAGE ON SCHEMA public TO meshychat_app WITH GRANT OPTION';
        ELSIF rec.old_has AND rec.new_has AND NOT rec.old_go AND rec.new_go THEN
            EXECUTE 'REVOKE GRANT OPTION FOR USAGE ON SCHEMA public FROM meshychat_app';
        END IF;
    END LOOP;

    FOR rec IN
        SELECT c.oid, c.relkind, n.nspname, c.relname, a.privilege_type,
               COALESCE(bool_or(a.grantee = old_role AND a.is_grantable), false) AS old_go,
               COALESCE(bool_or(a.grantee = new_role AND a.is_grantable), false) AS new_go,
               COALESCE(bool_or(a.grantee = old_role), false) AS old_has,
               COALESCE(bool_or(a.grantee = new_role), false) AS new_has
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
          LEFT JOIN LATERAL aclexplode(coalesce(c.relacl, acldefault(
              CASE WHEN c.relkind = 'S' THEN 'S'::"char" ELSE 'r'::"char" END, c.relowner))) a ON true
         WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
         GROUP BY c.oid, c.relkind, n.nspname, c.relname, a.privilege_type
    LOOP
        object_sql := CASE WHEN rec.relkind = 'S' THEN 'SEQUENCE '
                           ELSE 'TABLE ' END || quote_ident(rec.nspname) || '.' || quote_ident(rec.relname);
        IF rec.old_has AND NOT rec.new_has THEN
            EXECUTE 'GRANT ' || rec.privilege_type || ' ON ' || object_sql || ' TO meshychat_app' ||
                    CASE WHEN rec.old_go THEN ' WITH GRANT OPTION' ELSE '' END;
        ELSIF NOT rec.old_has AND rec.new_has THEN
            EXECUTE 'REVOKE ' || rec.privilege_type || ' ON ' || object_sql || ' FROM meshychat_app';
        ELSIF rec.old_has AND rec.new_has AND rec.old_go AND NOT rec.new_go THEN
            EXECUTE 'GRANT ' || rec.privilege_type || ' ON ' || object_sql || ' TO meshychat_app WITH GRANT OPTION';
        ELSIF rec.old_has AND rec.new_has AND NOT rec.old_go AND rec.new_go THEN
            EXECUTE 'REVOKE GRANT OPTION FOR ' || rec.privilege_type || ' ON ' || object_sql || ' FROM meshychat_app';
        END IF;
    END LOOP;

    FOR rec IN
        SELECT p.oid, n.nspname, p.proname, pg_get_function_identity_arguments(p.oid) AS args,
               a.privilege_type,
               COALESCE(bool_or(a.grantee = old_role AND a.is_grantable), false) AS old_go,
               COALESCE(bool_or(a.grantee = new_role AND a.is_grantable), false) AS new_go,
               COALESCE(bool_or(a.grantee = old_role), false) AS old_has,
               COALESCE(bool_or(a.grantee = new_role), false) AS new_has
          FROM pg_proc p
          JOIN pg_namespace n ON n.oid = p.pronamespace
          LEFT JOIN LATERAL aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a ON true
         WHERE n.nspname = 'public'
         GROUP BY p.oid, n.nspname, p.proname, a.privilege_type
    LOOP
        object_sql := 'FUNCTION ' || quote_ident(rec.nspname) || '.' || quote_ident(rec.proname) || '(' || rec.args || ')';
        IF rec.old_has AND NOT rec.new_has THEN
            EXECUTE 'GRANT ' || rec.privilege_type || ' ON ' || object_sql || ' TO meshychat_app' ||
                    CASE WHEN rec.old_go THEN ' WITH GRANT OPTION' ELSE '' END;
        ELSIF NOT rec.old_has AND rec.new_has THEN
            EXECUTE 'REVOKE ' || rec.privilege_type || ' ON ' || object_sql || ' FROM meshychat_app';
        ELSIF rec.old_has AND rec.new_has AND rec.old_go AND NOT rec.new_go THEN
            EXECUTE 'GRANT ' || rec.privilege_type || ' ON ' || object_sql || ' TO meshychat_app WITH GRANT OPTION';
        ELSIF rec.old_has AND rec.new_has AND NOT rec.old_go AND rec.new_go THEN
            EXECUTE 'REVOKE GRANT OPTION FOR ' || rec.privilege_type || ' ON ' || object_sql || ' FROM meshychat_app';
        END IF;
    END LOOP;

    FOR rec IN
        SELECT d.defaclrole, d.defaclobjtype, a.privilege_type,
               COALESCE(bool_or(a.grantee = old_role AND a.is_grantable), false) AS old_go,
               COALESCE(bool_or(a.grantee = new_role AND a.is_grantable), false) AS new_go,
               COALESCE(bool_or(a.grantee = old_role), false) AS old_has,
               COALESCE(bool_or(a.grantee = new_role), false) AS new_has
          FROM pg_default_acl d
          JOIN pg_namespace n ON n.oid = d.defaclnamespace
          LEFT JOIN LATERAL aclexplode(d.defaclacl) a ON true
         WHERE n.nspname = 'public'
         GROUP BY d.defaclrole, d.defaclobjtype, a.privilege_type
    LOOP
        object_sql := CASE rec.defaclobjtype WHEN 'r' THEN 'TABLES'
                                             WHEN 'S' THEN 'SEQUENCES'
                                             WHEN 'f' THEN 'FUNCTIONS' END;
        IF object_sql IS NULL THEN CONTINUE; END IF;
        grant_suffix := 'ALTER DEFAULT PRIVILEGES FOR ROLE ' || quote_ident((SELECT rolname FROM pg_roles WHERE oid = rec.defaclrole)) ||
                        ' IN SCHEMA public ';
        IF rec.old_has AND NOT rec.new_has THEN
            EXECUTE grant_suffix || 'GRANT ' || rec.privilege_type || ' ON ' || object_sql || ' TO meshychat_app' ||
                    CASE WHEN rec.old_go THEN ' WITH GRANT OPTION' ELSE '' END;
        ELSIF NOT rec.old_has AND rec.new_has THEN
            EXECUTE grant_suffix || 'REVOKE ' || rec.privilege_type || ' ON ' || object_sql || ' FROM meshychat_app';
        ELSIF rec.old_has AND rec.new_has AND rec.old_go AND NOT rec.new_go THEN
            EXECUTE grant_suffix || 'GRANT ' || rec.privilege_type || ' ON ' || object_sql || ' TO meshychat_app WITH GRANT OPTION';
        ELSIF rec.old_has AND rec.new_has AND NOT rec.old_go AND rec.new_go THEN
            EXECUTE grant_suffix || 'REVOKE GRANT OPTION FOR ' || rec.privilege_type || ' ON ' || object_sql || ' FROM meshychat_app';
        END IF;
    END LOOP;

    -- meshychat_app now holds everything meshy_app held. Retire meshy_app's privileges in
    -- THIS database so a database built from the history ends with no meshy role in its
    -- ACLs (a dump of it then names only meshychat_app and the owner). The role, its
    -- membership and its comment stay: they are cluster-wide and belong to the window.
    -- Measured at version 45: meshy_app holds public-schema grants only (schema, tables,
    -- sequences, functions, default ACLs); no column, type or database grants.
    -- Applying this migration while instances still connect as meshy_app strips their
    -- privileges, but the renames below break those instances already, so the window's
    -- order (role script first, then this migration) is the only safe one.
    REVOKE ALL PRIVILEGES ON SCHEMA public FROM meshy_app;
    REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM meshy_app;
    REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM meshy_app;
    REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public FROM meshy_app;
    FOR rec IN
        SELECT DISTINCT d.defaclrole, d.defaclobjtype
          FROM pg_default_acl d
          JOIN pg_namespace n ON n.oid = d.defaclnamespace
         WHERE n.nspname = 'public' AND d.defaclobjtype IN ('r', 'S', 'f')
    LOOP
        EXECUTE 'ALTER DEFAULT PRIVILEGES FOR ROLE ' ||
                quote_ident((SELECT rolname FROM pg_roles WHERE oid = rec.defaclrole)) ||
                ' IN SCHEMA public REVOKE ALL ON ' ||
                CASE rec.defaclobjtype WHEN 'r' THEN 'TABLES' WHEN 'S' THEN 'SEQUENCES' ELSE 'FUNCTIONS' END ||
                ' FROM meshy_app';
    END LOOP;
END
$p2_roles$;

-- P2 objects: rename every database object named meshy_* to meshychat_* (up).
-- Generated by gen_p2.py from a database migrated to main ee49985 (both sets, sql_meshychat 034).
-- Runs inside p2_up.sql or p2_down.sql, never alone. Every statement names its source exactly,
-- so a missing source or an existing target aborts the whole transaction and changes nothing.
-- Data VALUES are not touched (skill_call_events.source keeps 'meshy'; see README). Comments
-- are prose and wait for P4.

-- 1. tables (row types follow)
ALTER TABLE public.meshy_guard_approvals RENAME TO meshychat_guard_approvals;
ALTER TABLE public.meshy_guard_flow_decisions RENAME TO meshychat_guard_flow_decisions;
ALTER TABLE public.meshy_guard_policies RENAME TO meshychat_guard_policies;
ALTER TABLE public.meshy_guard_policy_attachments RENAME TO meshychat_guard_policy_attachments;
ALTER TABLE public.meshy_guard_policy_decisions RENAME TO meshychat_guard_policy_decisions;
ALTER TABLE public.meshy_guard_reasoning_traces RENAME TO meshychat_guard_reasoning_traces;
ALTER TABLE public.meshy_guard_scan_results RENAME TO meshychat_guard_scan_results;
ALTER TABLE public.meshy_memory RENAME TO meshychat_memory;
-- 2. sequences
ALTER SEQUENCE public.meshy_guard_flow_decisions_id_seq RENAME TO meshychat_guard_flow_decisions_id_seq;
ALTER SEQUENCE public.meshy_guard_policy_decisions_decision_id_seq RENAME TO meshychat_guard_policy_decisions_decision_id_seq;
ALTER SEQUENCE public.meshy_guard_reasoning_traces_id_seq RENAME TO meshychat_guard_reasoning_traces_id_seq;
ALTER SEQUENCE public.meshy_guard_scan_results_id_seq RENAME TO meshychat_guard_scan_results_id_seq;
-- 3. columns
ALTER TABLE public.meshychat_memory RENAME COLUMN meshy_guard_artifact_hash TO meshychat_guard_artifact_hash;
ALTER TABLE public.meshychat_memory RENAME COLUMN meshy_guard_review TO meshychat_guard_review;
ALTER TABLE public.meshychat_memory RENAME COLUMN meshy_guard_reviewed_at TO meshychat_guard_reviewed_at;
ALTER TABLE public.meshychat_memory RENAME COLUMN meshy_guard_status TO meshychat_guard_status;
ALTER TABLE public.skills RENAME COLUMN meshy_guard_artifact_hash TO meshychat_guard_artifact_hash;
ALTER TABLE public.skills RENAME COLUMN meshy_guard_review TO meshychat_guard_review;
ALTER TABLE public.skills RENAME COLUMN meshy_guard_reviewed_at TO meshychat_guard_reviewed_at;
ALTER TABLE public.skills RENAME COLUMN meshy_guard_status TO meshychat_guard_status;
-- 4. constraints (a primary key or unique constraint renames its index too)
ALTER TABLE public.meshychat_guard_approvals RENAME CONSTRAINT meshy_guard_approvals_expiry_after_create TO meshychat_guard_approvals_expiry_after_create;
ALTER TABLE public.meshychat_guard_approvals RENAME CONSTRAINT meshy_guard_approvals_id_format TO meshychat_guard_approvals_id_format;
ALTER TABLE public.meshychat_guard_approvals RENAME CONSTRAINT meshy_guard_approvals_phase_check TO meshychat_guard_approvals_phase_check;
ALTER TABLE public.meshychat_guard_approvals RENAME CONSTRAINT meshy_guard_approvals_pkey TO meshychat_guard_approvals_pkey;
ALTER TABLE public.meshychat_guard_approvals RENAME CONSTRAINT meshy_guard_approvals_preview_len TO meshychat_guard_approvals_preview_len;
ALTER TABLE public.meshychat_guard_approvals RENAME CONSTRAINT meshy_guard_approvals_resolution_shape TO meshychat_guard_approvals_resolution_shape;
ALTER TABLE public.meshychat_guard_approvals RENAME CONSTRAINT meshy_guard_approvals_sandbox_status_check TO meshychat_guard_approvals_sandbox_status_check;
ALTER TABLE public.meshychat_guard_approvals RENAME CONSTRAINT meshy_guard_approvals_status_check TO meshychat_guard_approvals_status_check;
ALTER TABLE public.meshychat_guard_approvals RENAME CONSTRAINT meshy_guard_approvals_status_verdict TO meshychat_guard_approvals_status_verdict;
ALTER TABLE public.meshychat_guard_approvals RENAME CONSTRAINT meshy_guard_approvals_verdict_check TO meshychat_guard_approvals_verdict_check;
ALTER TABLE public.meshychat_guard_flow_decisions RENAME CONSTRAINT meshy_guard_flow_decisions_args_sha256_fmt TO meshychat_guard_flow_decisions_args_sha256_fmt;
ALTER TABLE public.meshychat_guard_flow_decisions RENAME CONSTRAINT meshy_guard_flow_decisions_decision_check TO meshychat_guard_flow_decisions_decision_check;
ALTER TABLE public.meshychat_guard_flow_decisions RENAME CONSTRAINT meshy_guard_flow_decisions_mode_check TO meshychat_guard_flow_decisions_mode_check;
ALTER TABLE public.meshychat_guard_flow_decisions RENAME CONSTRAINT meshy_guard_flow_decisions_pkey TO meshychat_guard_flow_decisions_pkey;
ALTER TABLE public.meshychat_guard_policies RENAME CONSTRAINT meshy_guard_policies_id_shape TO meshychat_guard_policies_id_shape;
ALTER TABLE public.meshychat_guard_policies RENAME CONSTRAINT meshy_guard_policies_kind_check TO meshychat_guard_policies_kind_check;
ALTER TABLE public.meshychat_guard_policies RENAME CONSTRAINT meshy_guard_policies_name_key TO meshychat_guard_policies_name_key;
ALTER TABLE public.meshychat_guard_policies RENAME CONSTRAINT meshy_guard_policies_pkey TO meshychat_guard_policies_pkey;
ALTER TABLE public.meshychat_guard_policy_attachments RENAME CONSTRAINT meshy_guard_policy_attachments_pkey TO meshychat_guard_policy_attachments_pkey;
ALTER TABLE public.meshychat_guard_policy_attachments RENAME CONSTRAINT meshy_guard_policy_attachments_policy_id_fkey TO meshychat_guard_policy_attachments_policy_id_fkey;
ALTER TABLE public.meshychat_guard_policy_decisions RENAME CONSTRAINT meshy_guard_policy_decisions_pkey TO meshychat_guard_policy_decisions_pkey;
ALTER TABLE public.meshychat_guard_reasoning_traces RENAME CONSTRAINT meshy_guard_reasoning_traces_pkey TO meshychat_guard_reasoning_traces_pkey;
ALTER TABLE public.meshychat_guard_scan_results RENAME CONSTRAINT meshy_guard_scan_results_conclusion_check TO meshychat_guard_scan_results_conclusion_check;
ALTER TABLE public.meshychat_guard_scan_results RENAME CONSTRAINT meshy_guard_scan_results_pkey TO meshychat_guard_scan_results_pkey;
ALTER TABLE public.meshychat_guard_scan_results RENAME CONSTRAINT meshy_guard_scan_results_review_status_check TO meshychat_guard_scan_results_review_status_check;
ALTER TABLE public.meshychat_memory RENAME CONSTRAINT meshy_memory_created_by_owner_id_fkey TO meshychat_memory_created_by_owner_id_fkey;
ALTER TABLE public.meshychat_memory RENAME CONSTRAINT meshy_memory_created_by_peer_id_fkey TO meshychat_memory_created_by_peer_id_fkey;
ALTER TABLE public.meshychat_memory RENAME CONSTRAINT meshy_memory_pkey TO meshychat_memory_pkey;
ALTER TABLE public.meshychat_memory RENAME CONSTRAINT meshy_memory_meshy_guard_status_check TO meshychat_memory_meshychat_guard_status_check;
ALTER TABLE public.meshychat_memory RENAME CONSTRAINT meshy_memory_source_check TO meshychat_memory_source_check;
ALTER TABLE public.skills RENAME CONSTRAINT skills_meshy_guard_status_check TO skills_meshychat_guard_status_check;
-- 5. indexes that back no constraint
ALTER INDEX public.meshy_guard_approvals_call_idx RENAME TO meshychat_guard_approvals_call_idx;
ALTER INDEX public.meshy_guard_approvals_caller_idx RENAME TO meshychat_guard_approvals_caller_idx;
ALTER INDEX public.meshy_guard_approvals_owner_pending_idx RENAME TO meshychat_guard_approvals_owner_pending_idx;
ALTER INDEX public.meshy_guard_approvals_pending_expiry_idx RENAME TO meshychat_guard_approvals_pending_expiry_idx;
ALTER INDEX public.meshy_guard_flow_decisions_call_idx RENAME TO meshychat_guard_flow_decisions_call_idx;
ALTER INDEX public.meshy_guard_flow_decisions_caller_time_idx RENAME TO meshychat_guard_flow_decisions_caller_time_idx;
ALTER INDEX public.meshy_guard_flow_decisions_reason_time_idx RENAME TO meshychat_guard_flow_decisions_reason_time_idx;
ALTER INDEX public.meshy_guard_flow_decisions_target_time_idx RENAME TO meshychat_guard_flow_decisions_target_time_idx;
ALTER INDEX public.meshy_guard_reasoning_traces_chain_uniq RENAME TO meshychat_guard_reasoning_traces_chain_uniq;
ALTER INDEX public.meshy_guard_scan_results_chain_idx RENAME TO meshychat_guard_scan_results_chain_idx;
ALTER INDEX public.meshy_guard_scan_results_chain_wm_uniq RENAME TO meshychat_guard_scan_results_chain_wm_uniq;
ALTER INDEX public.meshy_guard_scan_results_pending_idx RENAME TO meshychat_guard_scan_results_pending_idx;
ALTER INDEX public.meshy_memory_owner_idx RENAME TO meshychat_memory_owner_idx;
ALTER INDEX public.meshy_memory_meshy_guard_status_idx RENAME TO meshychat_memory_meshychat_guard_status_idx;
ALTER INDEX public.skills_meshy_guard_status_idx RENAME TO skills_meshychat_guard_status_idx;
-- 5b. index column names: an index keeps the column names it was built with, so a
--     column rename (or a function rename, for an expression index) does not reach them
ALTER TABLE public.idx_shared_memory_value_trgm RENAME COLUMN meshy_jsonb_values_text TO meshychat_jsonb_values_text;
ALTER TABLE public.meshychat_memory_meshychat_guard_status_idx RENAME COLUMN meshy_guard_status TO meshychat_guard_status;
ALTER TABLE public.skills_meshychat_guard_status_idx RENAME COLUMN meshy_guard_status TO meshychat_guard_status;
-- 6. row-level security policies
ALTER POLICY meshy_memory_internal_delete ON public.meshychat_memory RENAME TO meshychat_memory_internal_delete;
ALTER POLICY meshy_memory_internal_insert ON public.meshychat_memory RENAME TO meshychat_memory_internal_insert;
ALTER POLICY meshy_memory_internal_update ON public.meshychat_memory RENAME TO meshychat_memory_internal_update;
ALTER POLICY meshy_memory_tenant_select ON public.meshychat_memory RENAME TO meshychat_memory_tenant_select;
-- 7. triggers
ALTER TRIGGER meshy_memory_set_updated_at ON public.meshychat_memory RENAME TO meshychat_memory_set_updated_at;
-- 8. functions (policies and the trigram index bind by OID and follow)
ALTER FUNCTION public.meshy_jsonb_values_text(doc jsonb) RENAME TO meshychat_jsonb_values_text;
ALTER FUNCTION public.meshy_namespace_permission(namespace text, peer uuid) RENAME TO meshychat_namespace_permission;
ALTER FUNCTION public.meshy_namespace_permission(peer uuid) RENAME TO meshychat_namespace_permission;
ALTER FUNCTION public.meshy_same_tenant_or_devgroup(caller uuid, target uuid) RENAME TO meshychat_same_tenant_or_devgroup;
-- 9. function BODIES that name a renamed identifier. A body is text, so a rename
--    alone leaves it calling the old name; shared_memory_signed_bytes_agree compares
--    current_user to a role name, and without this its E_SIGNED_BYTES_REQUIRED check
--    would stop firing silently after the role rename.
DO $p2$
DECLARE
    d text;
    n int;
BEGIN
    d := pg_get_functiondef('public.meshychat_namespace_permission(uuid)'::regprocedure);
    n := (length(d) - length(replace(d, 'meshy_namespace_permission.peer', ''))) / length('meshy_namespace_permission.peer');
    IF n <> 3 THEN RAISE EXCEPTION 'P2: meshychat_namespace_permission: expected 3 of [%], found %', 'meshy_namespace_permission.peer', n; END IF;
    d := replace(d, 'meshy_namespace_permission.peer', 'meshychat_namespace_permission.peer');
    EXECUTE d;
    d := pg_get_functiondef('public.meshychat_namespace_permission(text, uuid)'::regprocedure);
    n := (length(d) - length(replace(d, 'meshy_namespace_permission.namespace', ''))) / length('meshy_namespace_permission.namespace');
    IF n <> 2 THEN RAISE EXCEPTION 'P2: meshychat_namespace_permission: expected 2 of [%], found %', 'meshy_namespace_permission.namespace', n; END IF;
    d := replace(d, 'meshy_namespace_permission.namespace', 'meshychat_namespace_permission.namespace');
    n := (length(d) - length(replace(d, 'FROM meshy_namespace_permission(meshy_namespace_permission.peer)', ''))) / length('FROM meshy_namespace_permission(meshy_namespace_permission.peer)');
    IF n <> 1 THEN RAISE EXCEPTION 'P2: meshychat_namespace_permission: expected 1 of [%], found %', 'FROM meshy_namespace_permission(meshy_namespace_permission.peer)', n; END IF;
    d := replace(d, 'FROM meshy_namespace_permission(meshy_namespace_permission.peer)', 'FROM meshychat_namespace_permission(meshychat_namespace_permission.peer)');
    EXECUTE d;
    d := pg_get_functiondef('public.shared_memory_signed_bytes_agree()'::regprocedure);
    n := (length(d) - length(replace(d, 'IF current_user = ''meshy_app''', ''))) / length('IF current_user = ''meshy_app''');
    IF n <> 1 THEN RAISE EXCEPTION 'P2: shared_memory_signed_bytes_agree: expected 1 of [%], found %', 'IF current_user = ''meshy_app''', n; END IF;
    d := replace(d, 'IF current_user = ''meshy_app''', 'IF current_user = ''meshychat_app''');
    n := (length(d) - length(replace(d, '''E_SIGNED_BYTES_REQUIRED: meshy_app must store', ''))) / length('''E_SIGNED_BYTES_REQUIRED: meshy_app must store');
    IF n <> 1 THEN RAISE EXCEPTION 'P2: shared_memory_signed_bytes_agree: expected 1 of [%], found %', '''E_SIGNED_BYTES_REQUIRED: meshy_app must store', n; END IF;
    d := replace(d, '''E_SIGNED_BYTES_REQUIRED: meshy_app must store', '''E_SIGNED_BYTES_REQUIRED: meshychat_app must store');
    EXECUTE d;
END
$p2$;
COMMIT;

