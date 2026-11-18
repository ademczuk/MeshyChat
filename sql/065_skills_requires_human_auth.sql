-- 065_skills_requires_human_auth.sql
-- TASK-30 AC#1: a high-risk skill can require LIVE per-call human authorization.
--
-- Every cross-owner grant + skill_call is agent-autonomous today: the envelope
-- signature proves WHICH peer signed, never WHETHER a human authorized this
-- specific call. This flag marks a skill as needing a fresh human-consent
-- signature at invoke time (the gate lands in meshy-calls authorize, deploy-dark:
-- it fires only when the flag is set AND a consent proof is presented).
--
-- Additive + backward-compatible: DEFAULT FALSE means every existing skill is
-- behavior-identical (mirrors attachments_allowed / returns_attachments in
-- 003_skills.sql). Only an owner opting a skill in changes its behavior. The
-- flag is also pinned into the manifest hash (meshy-grants manifest.rs ManifestView)
-- so a publisher cannot silently flip it off after a grantee approved a
-- human-required skill (the rug-pull class the manifest hash defends, TASK-21).

ALTER TABLE skills
    ADD COLUMN requires_human_auth BOOLEAN NOT NULL DEFAULT FALSE;
