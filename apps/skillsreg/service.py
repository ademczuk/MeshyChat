"""Domain rules for the skill registry.

THE ORDER OF THE CHECKS IS PART OF THE CONTRACT, not an implementation detail. A client
that posts a request wrong in two ways at once gets a specific status and message back,
and reordering these turns a 400 into a 409 or the reverse. Each function below runs
them in that order so the sequence stays reviewable.

Errors are raised as `ApiError` (the envelope class `errors.py` shares with `apps/peers`),
which carries the (status, code, message) triple the wire needs; `views.py` never invents
one.
"""
from __future__ import annotations

import datetime as _dt
import logging

import psycopg

from apps.skillsreg import lessons, manifest, repository, schema_validate
from apps.skillsreg import runtime as rt
from apps.skillsreg import semver
from apps.skillsreg.errors import conflict, internal, not_found, validation
from apps.core.env import lookup

log = logging.getLogger("meshychat.skillsreg")

#: Postgres unique-violation. Re-declaring a previously SOFT-DELETED
#: (name, version) trips `skills_peer_id_name_version_key`, because the live-current
#: guard's WHERE requires `deleted_at IS NULL` and cannot see the deleted row.
UNIQUE_VIOLATION = "23505"


def _peer_dbg(peer_id) -> str:
    """Render a peer id as `PeerId(<hyphenated uuid>)` for the wire message."""
    return f"PeerId({peer_id})"


# --------------------------------------------------------------------- the gates

#: Values that RELAX schema enforcement. Anything else - including an unset variable, an
#: empty string and a typo - enforces.
#:
#: THE DIRECTION IS THE POINT. Divergence 010 keeps enforcement ON by default and permits a
#: flag only to relax it. A default-off schema gate would fail open for an SSRF control: an
#: `input_schema` carrying `$ref: https://evil.example.test/x` would return 201 and be stored.
#:
#: A TYPO MUST FAIL CLOSED. `MESHYCHAT_SKILL_SCHEMA_ENFORCE=flase` enforces, because the only way
#: to turn a security control off should be to spell "off" correctly.
SCHEMA_ENFORCE_RELAXING_VALUES = frozenset({"0", "false", "off", "no", "observe"})


def schema_enforce() -> bool:
    """True unless the operator explicitly relaxed it. Read at CALL time, not import, so
    flipping it is a process restart rather than reimporting the module graph - and so one
    test run can exercise both arms."""
    raw = lookup("MESHYCHAT_SKILL_SCHEMA_ENFORCE")
    if raw is None:
        return True
    return raw.strip().lower() not in SCHEMA_ENFORCE_RELAXING_VALUES


def _schema_gate(peer_id, name, input_schema, output_schema) -> None:
    """Refuse an invalid schema unless the schema setting explicitly relaxes enforcement.

    The flip was measured before it was made, not argued: every stored skill in the
    development database - 6,604 of them - was passed through `validate_skill_schema` and
    NONE would be refused, with a positive control in the same run showing the validator
    does reject an external `$ref`. A zero from a validator that refuses nothing would have
    been a broken instrument rather than a clean corpus.
    """
    try:
        schema_validate.validate_skill_schema(input_schema, output_schema)
    except schema_validate.SchemaValidateError as exc:
        if schema_enforce():
            raise validation(f"skill schema invalid: {exc}")
        log.warning("skill_schema_invalid (ACCEPTED because MESHYCHAT_SKILL_SCHEMA_ENFORCE "
                    "explicitly relaxes it): peer=%s skill=%s reason=%s",
                    peer_id, name, exc)


def manifest_attestation_enforce() -> bool:
    """Read the shared manifest-attestation setting, parsed once at boot.

    `apps/core/attestation_flag.py` makes unset false and accepts only exactly `true` or
    `false`."""
    from apps.core import attestation_flag

    return attestation_flag.enforce_from_settings()


def _attestation_gate(attestation_pair) -> None:
    """Require an attestation pair when the shared setting enables enforcement."""
    if manifest_attestation_enforce() and attestation_pair is None:
        raise validation("manifest attestation required")


def _paid_consistency(share_mode, price_cents, currency) -> None:
    """Require price_cents and currency when share_mode is marketplace_paid.

    Mirrored by the DB's own `skills_check` CHECK constraint,
    which is why a bug here surfaces as a 500 rather than a silently bad row."""
    if share_mode == "marketplace_paid" and (price_cents is None or currency is None):
        raise validation("share_mode = marketplace_paid requires price_cents and currency")


def _prepare_runtime_fields(runtime_profile, runtime_status):
    """Validate, then produce the two JSONB storage values.

    The profile is stored NORMALIZED (empty collapses to NULL so legacy manifest hashes
    stay byte-identical); the status is stored with the SERVER clock stamped onto any
    declared lease, and the client's `lease_declared_at` is discarded.
    """
    try:
        rt.validate_runtime_fields(runtime_profile, runtime_status)
    except rt.RuntimeValidateError as exc:
        # A client-declared vocabulary problem is a 400. Only a
        # SERIALIZE failure would be a 500, and there is no separate serialize step
        # here - `json.dumps` inside the validator already raised if it could not.
        raise validation(str(exc))
    profile = rt.normalized_profile(runtime_profile)
    profile_value = None if profile is None else rt.profile_to_wire(profile)
    status_value = rt.status_to_wire(rt.stamp_status(
        runtime_status, _dt.datetime.now(_dt.timezone.utc)))
    return profile_value, status_value


def _lesson_gates(conn, mode, examples, peer_id) -> None:
    """Run both lesson checks, in order, only for `mode == lesson`."""
    if mode != "lesson":
        return
    try:
        lessons.validate_lesson_consistency(examples, None)
        lessons.validate_lesson_signature(conn, examples, peer_id)
    except lessons.LessonError as exc:
        raise validation(str(exc))


def _publish_gates(conn, peer_id, name, req):
    """The gate preamble `create_skill` and `release_version` share, in this order:
    semver, paid consistency, lesson, schema, attestation,
    runtime fields. Replaces two verbatim copies of the six statements; each caller's
    statuses, messages and sequence are unchanged. `name` is the one thing that differed:
    `req["name"]` on declare, the path segment on release. Returns the
    `(profile_value, status_value)` pair `_prepare_runtime_fields` produces, because the
    runtime gate is the last one and its output is what the insert stores.
    """
    try:
        semver.validate_semver(req["version"])
    except semver.InvalidVersion as exc:
        raise validation(exc.message)
    _paid_consistency(req["share_mode"], req["price_cents"], req["currency"])
    _lesson_gates(conn, req["mode"], req["examples"], peer_id)
    _schema_gate(peer_id, name, req["input_schema"], req["output_schema"])
    _attestation_gate(req["attestation"])
    return _prepare_runtime_fields(req["runtime_profile"], req["runtime_status"])


def _insert_from_request(cur, peer_id, name, req, profile_value, status_value) -> dict:
    """The `repository.insert` call `create_skill` and `release_version` share: every
    column from the validated request dict, the two prepared runtime values, and the
    attestation pair unpacked (both NULL when absent). Replaces two byte-identical kwargs
    blocks; the row each caller inserts is unchanged.
    """
    manifest_sig, signing_kid = req["attestation"] or (None, None)
    return repository.insert(
        cur, peer_id,
        name=name, version=req["version"],
        display_name=req["display_name"], description=req["description"],
        mode=req["mode"], share_mode=req["share_mode"],
        input_schema=req["input_schema"], output_schema=req["output_schema"],
        attachments_allowed=req["attachments_allowed"],
        returns_attachments=req["returns_attachments"],
        requires_human_auth=req["requires_human_auth"],
        max_runtime_ms=req["max_runtime_ms"], price_cents=req["price_cents"],
        currency=req["currency"], tags=req["tags"], examples=req["examples"],
        runtime_profile=profile_value, runtime_status=status_value,
        manifest_sig=manifest_sig, signing_kid=signing_kid)


# ------------------------------------------------------------------- create_skill

def create_skill(conn, peer_id, req) -> dict:
    """Create a skill from the validated `DeclareSkillRequest` dict."""
    profile_value, status_value = _publish_gates(conn, peer_id, req["name"], req)

    existing = repository.find_current_by_name(conn, peer_id, req["name"])
    if existing is not repository.NOT_FOUND:
        raise conflict(f"skill {req['name']} already exists for peer {_peer_dbg(peer_id)}")

    try:
        with conn.transaction():
            with conn.cursor() as cur:
                skill = _insert_from_request(cur, peer_id, req["name"], req,
                                             profile_value, status_value)
    except psycopg.errors.UniqueViolation:
        # The live-current guard above cannot see soft-deleted rows; the UNIQUE
        # constraint counts them. 409, not 500.
        raise conflict(f"skill {req['name']} already exists for peer {_peer_dbg(peer_id)}")
    except psycopg.errors.CheckViolation as exc:
        # The DB is the second enforcer of the paid/attestation-pair rules. Reaching
        # here means a gate above disagrees with a CHECK constraint, which is a defect
        # in THIS code - report it as one rather than as a client error.
        raise internal(f"skills CHECK constraint rejected a row the service accepted: {exc}")

    # Lesson fan-out belongs here, AFTER commit, so a rolled-back row is never broadcast.
    # NOT IMPLEMENTED - see design note 3 in the views.py module docstring. The Redis
    # fan-out belongs to the wsgw surface, not this one.
    return skill


# ---------------------------------------------------------------- release_version

def release_version(conn, peer_id, name, req) -> dict:
    """Release a new version for an existing skill."""
    profile_value, status_value = _publish_gates(conn, peer_id, name, req)

    current = repository.find_current_by_name(conn, peer_id, name)
    if current is repository.NOT_FOUND:
        raise not_found()

    # NUMERIC three-part ordering, never string order: string order rejects
    # double-digit majors (10.0.0 below 9.0.0), which is #410. The STORED side is
    # parsed leniently (two-part legacy form padded) and its failure message names the
    # STORED version, or the error blames the new version for a defect in the old row.
    try:
        current_parsed = semver.parse_stored_semver(current["version"])
    except semver.InvalidVersion:
        raise validation(semver.InvalidVersion(
            "stored current version is not numeric semver (cannot release over it): "
            f"{current['version']}").message)
    new_parsed = semver.parse_semver(req["version"])
    if new_parsed <= current_parsed:
        raise conflict(
            f"cannot release {req['version']} for {_peer_dbg(peer_id)}/{name} - "
            f"current version is {current['version']}")

    # ONE TRANSACTION, and this is the reason the connection must be autocommit (db.py).
    # A separately-committed flip that is followed by a failed insert leaves the skill
    # with NO is_current=TRUE row: it vanishes from list/resolve/search and every later
    # release returns NotFound - a wedge needing manual DB repair.
    #
    # THE 23505 HANDLER IS DELIBERATE. `release_version` and `create_skill` both answer
    # 409 for this duplicate: the live-current lookup above requires `deleted_at IS NULL`
    # and cannot see a soft-deleted row while the unique key counts it. Returning 500 here
    # would make duplicate handling inconsistent on the wire.
    #
    # The `except` is OUTSIDE `conn.transaction()` on purpose: psycopg re-raises out of
    # the context manager after rolling back, so by the time this runs the flip is
    # already undone. Catching it INSIDE would leave the aborted transaction to be
    # exited with no error and the rollback would still happen, but the flip's
    # reinstatement would no longer be what this code is asserting.
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                repository.flip_previous_current(cur, peer_id, name)
                skill = _insert_from_request(cur, peer_id, name, req,
                                             profile_value, status_value)
    except psycopg.errors.UniqueViolation:
        # Same constraint, same message as `create_skill`'s handler, because it is the
        # same duplicate: (peer_id, name, version) already exists on a row the
        # live-current guard could not see.
        raise conflict(f"skill {name} already exists for peer {_peer_dbg(peer_id)}")
    return skill


# ---------------------------------------------------------------- update_metadata

def update_metadata(conn, peer_id, name, req) -> dict:
    """Update the metadata of a current skill.

    THE SIGNATURE DECISION IS THE INTERESTING PART. `description`, `tags`,
    `attachments_allowed`, `returns_attachments`, `requires_human_auth` and
    `runtime_profile` are manifest-hash-covered - the same fields a grant's version pin
    approves. If applying this update would actually CHANGE the hash, the attestation
    gate re-runs and the stored signature is either replaced with the fresh one or
    NULLED. Leaving a signature that verifies the OLD hash in place would make the new
    content look attested. A purely operational or commercial change (price, currency,
    display_name, max_runtime_ms, examples, share_mode) never touches the hash and
    leaves the signature alone.
    """
    _paid_consistency(req["share_mode"], req["price_cents"], req["currency"])
    profile_value, status_value = _prepare_runtime_fields(
        req["runtime_profile"], req["runtime_status"])

    signature_update = None
    existing = repository.find_current_by_name(conn, peer_id, name)
    if existing is not repository.NOT_FOUND:
        _lesson_gates(conn, existing["mode"], req["examples"], peer_id)

        effective_human_auth = (existing["requires_human_auth"]
                                if req["requires_human_auth"] is None
                                else req["requires_human_auth"])
        new_hash = manifest.manifest_hash_for_fields(
            existing["peer_id"], existing["name"], existing["version"], existing["mode"],
            req["description"], existing["input_schema"], existing["output_schema"],
            req["attachments_allowed"], req["returns_attachments"], effective_human_auth,
            req["tags"], req["runtime_profile"])
        old_hash = manifest.manifest_hash(existing)
        if new_hash != old_hash:
            _attestation_gate(req["attestation"])
            signature_update = req["attestation"] or (None, None)

    updated = repository.update_metadata(
        conn, peer_id, name,
        share_mode=req["share_mode"], display_name=req["display_name"],
        description=req["description"], tags=req["tags"],
        max_runtime_ms=req["max_runtime_ms"], price_cents=req["price_cents"],
        currency=req["currency"], attachments_allowed=req["attachments_allowed"],
        returns_attachments=req["returns_attachments"],
        requires_human_auth=req["requires_human_auth"], examples=req["examples"],
        runtime_profile=profile_value, runtime_status=status_value,
        signature_update=signature_update)
    if updated is repository.NOT_FOUND:
        # A no-row update returns 404. This is also the arm a
        # CROSS-PEER update lands in: the UPDATE's `WHERE peer_id = <authenticated>`
        # matched no row, so the skill's existence is not disclosed either.
        raise not_found()
    return updated


def soft_delete(conn, peer_id, name) -> int:
    """Soft-delete a skill; a no-row result returns 404."""
    return repository.soft_delete(conn, peer_id, name)
