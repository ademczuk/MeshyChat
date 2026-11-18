"""SQL against `skills`.

Spec 5.2: NEVER hard-delete a `skills` row while in-flight or historical
`skill_calls.skill_id` rows reference it. `deleted_at` exclusively.

TYPED ABSENCE, NOT AN EMPTY COLLECTION. Every read below returns either a row (a dict)
or one of the `Absence` singletons at the top of this file. It never returns a bare
`None`, `[]` or `{}`, and there is no `except: return []` anywhere in this app. The
reason is specific to this schema rather than general tidiness: a scoped read that finds
nothing and a read issued without the context it needed are the SAME empty result, and
the second one is a bug that looks like data. `skills` has no RLS today (see `db.py`),
but `find_current_by_name` returning "nothing" already has three distinct causes -
the skill never existed, it was soft-deleted, or a NEW VERSION FLIP left it with no
`is_current = TRUE` row - and the service layer answers 404 / 409 differently for them.

NO PER-REQUEST FULL-TABLE OR FULL-CHAIN AGGREGATE. Every statement here is keyed on
`peer_id` (and usually `name`), which is covered by `skills_peer_name_deleted_idx` and
`skills_one_current_per_name`. There is no `COUNT(*)`, no unbounded scan, and no
"fetch them all and count in Python". The rule exists because a `COUNT(*)` over a whole
chain on every authorize once cost ~490 GB/day of disk reads.

The column list is a CONSTANT, projected identically by every statement, because
`_row_to_skill` indexes it positionally; a SELECT that reordered its columns would
silently mis-map `price_cents` onto `max_runtime_ms`.
"""
from __future__ import annotations

from psycopg.types.json import Jsonb


class Absence:
    """Why a read returned nothing. Two reasons, never conflated.

    No `__bool__` is defined on purpose. If `Absence` were falsy, `if row:` would
    compile and run and quietly treat EMPTY as NOT_FOUND - which is precisely the
    conflation this type exists to prevent. Callers must test identity.

    A third reason, DENIED, was declared here and deleted on 2026-09-29: nothing produced
    or consumed it, and its one test only compared it with NOT_FOUND (review queue).
    """

    __slots__ = ("reason", "detail")

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        self.detail = detail

    def __repr__(self) -> str:
        return f"Absence({self.reason!r}, {self.detail!r})"


#: The row does not exist, or is soft-deleted, or has no current version.
NOT_FOUND = Absence("not_found")
#: The query was well-formed, scoped correctly, and matched zero rows.
EMPTY = Absence("empty")

#: Every SELECT/RETURNING projects these, in this order.
COLUMNS = (
    "id", "peer_id", "name", "version", "display_name", "description", "mode",
    "share_mode", "input_schema", "output_schema", "attachments_allowed",
    "returns_attachments", "requires_human_auth", "max_runtime_ms", "price_cents",
    "currency", "tags", "examples", "manifest_sig", "signing_kid", "runtime_profile",
    "runtime_status", "is_current", "deleted_at", "created_at", "updated_at",
)
_COLS = ", ".join(COLUMNS)


def _jsonb_or_null(value):
    """`Jsonb(value)`, or SQL NULL for an absent value. Replaces the six inline
    `None if x is None else Jsonb(x)` expressions in `insert` and `update_metadata`; the
    parameters each statement binds are unchanged (`input_schema` stays an unconditional
    `Jsonb`, as it was)."""
    return None if value is None else Jsonb(value)


def _row_to_skill(row) -> dict:
    """Map a row to wire names in wire order."""
    skill = dict(zip(COLUMNS, row))
    sig = skill.get("manifest_sig")
    if sig is not None:
        # psycopg hands back `memoryview` for bytea; normalise once, here, so nothing
        # downstream has to know that.
        skill["manifest_sig"] = bytes(sig)
    skill["tags"] = list(skill.get("tags") or [])
    return skill


# ------------------------------------------------------------------------ reads

def find_current_by_name(conn, peer_id, name):
    """Return the current, non-deleted version, or NOT_FOUND."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLS} FROM skills "
            "WHERE peer_id = %s AND name = %s AND is_current = TRUE AND deleted_at IS NULL",
            (str(peer_id), name))
        row = cur.fetchone()
    return NOT_FOUND if row is None else _row_to_skill(row)


def list_for_peer(conn, peer_id, include_deleted: bool = False):
    """Return all versions for a peer, newest first, or EMPTY."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLS} FROM skills "
            "WHERE peer_id = %s AND (%s::bool OR deleted_at IS NULL) "
            "ORDER BY created_at DESC",
            (str(peer_id), include_deleted))
        rows = cur.fetchall()
    return EMPTY if not rows else [_row_to_skill(r) for r in rows]


def list_versions_for_skill(conn, peer_id, name):
    """Return all non-deleted versions of one skill, or EMPTY."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLS} FROM skills "
            "WHERE peer_id = %s AND name = %s AND deleted_at IS NULL "
            "ORDER BY created_at DESC",
            (str(peer_id), name))
        rows = cur.fetchall()
    return EMPTY if not rows else [_row_to_skill(r) for r in rows]


def peer_owner_id(conn, peer_id):
    """Resolve the viewer's owner for the grant check."""
    with conn.cursor() as cur:
        cur.execute("SELECT owner_id FROM peers WHERE id = %s", (str(peer_id),))
        row = cur.fetchone()
    return NOT_FOUND if row is None else row[0]


def has_active_grant(conn, grantee_owner_id, granter_peer_id, skill_name) -> bool:
    """The canonical grant-liveness predicate.

    `revoked_at IS NULL AND (expires_at IS NULL OR expires_at > NOW())`. The expiry
    half is NOT optional here even though the shared `list_held` query omits it: this
    is a security gate deciding whether a non-grantee learns a `grants_only` skill exists,
    so an expired-but-unrevoked grant must not keep it visible one moment longer than
    authorize() would let it be invoked.

    Written as `EXISTS (... LIMIT 1)` rather than a count. A `COUNT(*)` over a peer's
    grants would needlessly scan every match; the answer to "is there one" never needs
    the cardinality.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT EXISTS(SELECT 1 FROM skill_grants "
            "  WHERE grantee_owner_id = %s AND granter_peer_id = %s AND skill_name = %s "
            "    AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > NOW()) "
            "  LIMIT 1)",
            (str(grantee_owner_id), str(granter_peer_id), skill_name))
        return bool(cur.fetchone()[0])


# ----------------------------------------------------------------------- writes

_INSERT = f"""INSERT INTO skills
       (peer_id, name, version, display_name, description, mode, share_mode,
        input_schema, output_schema, attachments_allowed, returns_attachments,
        requires_human_auth, max_runtime_ms, price_cents, currency, tags,
        examples, runtime_profile, runtime_status, manifest_sig, signing_kid, is_current)
       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE)
       RETURNING {_COLS}"""


def insert(cur, peer_id, *, name, version, display_name, description, mode, share_mode,
           input_schema, output_schema, attachments_allowed, returns_attachments,
           requires_human_auth, max_runtime_ms, price_cents, currency, tags, examples,
           runtime_profile, runtime_status, manifest_sig, signing_kid) -> dict:
    """Insert a current skill row.

    Takes a CURSOR, not a connection: `release_version` must run this and
    `flip_previous_current` inside ONE transaction, so the transaction is the caller's
    to own.

    `peer_id` is the AUTHENTICATED peer, supplied by the caller from the verified token.
    It is never read from the request body, and `skills` has no INSERT policy to catch
    it if it were (see `db.py`) - so this signature makes `peer_id` positional and every
    other column keyword-only, which is what stops a future edit from sliding a
    body-supplied value into that slot by argument order.
    """
    cur.execute(_INSERT, (
        str(peer_id), name, version, display_name, description, mode, share_mode,
        Jsonb(input_schema),
        _jsonb_or_null(output_schema),
        attachments_allowed, returns_attachments, requires_human_auth, max_runtime_ms,
        price_cents, currency, list(tags or []),
        _jsonb_or_null(examples),
        _jsonb_or_null(runtime_profile),
        _jsonb_or_null(runtime_status),
        manifest_sig, signing_kid,
    ))
    return _row_to_skill(cur.fetchone())


def flip_previous_current(cur, peer_id, name) -> None:
    """Mark the previous current version as not current."""
    cur.execute(
        "UPDATE skills SET is_current = FALSE "
        "WHERE peer_id = %s AND name = %s AND is_current = TRUE AND deleted_at IS NULL",
        (str(peer_id), name))


_UPDATE = f"""UPDATE skills
       SET share_mode = %s, display_name = %s, description = %s, tags = %s,
           max_runtime_ms = %s, price_cents = %s, currency = %s,
           attachments_allowed = %s, returns_attachments = %s,
           requires_human_auth = COALESCE(%s, requires_human_auth), examples = %s,
           runtime_profile = %s, runtime_status = %s,
           manifest_sig = CASE WHEN %s THEN %s ELSE manifest_sig END,
           signing_kid = CASE WHEN %s THEN %s ELSE signing_kid END
       WHERE peer_id = %s AND name = %s AND is_current = TRUE AND deleted_at IS NULL
       RETURNING {_COLS}"""


def update_metadata(conn, peer_id, name, *, share_mode, display_name, description, tags,
                    max_runtime_ms, price_cents, currency, attachments_allowed,
                    returns_attachments, requires_human_auth, examples, runtime_profile,
                    runtime_status, signature_update):
    """Return the updated row, or NOT_FOUND.

    `signature_update` is None to LEAVE `manifest_sig`/`signing_kid` untouched (no
    hash-covered field is changing), or a `(sig, kid)` pair to overwrite them -
    `(None, None)` explicitly NULLS a now-stale signature. `service.update_metadata`
    decides which case applies; doing it here would put a security decision in the
    layer that cannot see the old manifest hash.

    The `WHERE peer_id = %s` is the whole cross-peer guard. A skill owned by another
    peer matches zero rows and this returns NOT_FOUND, which the view answers with 404.
    There is no row-level-security backstop behind it (see `db.py`).
    """
    touch_sig = signature_update is not None
    new_sig, new_kid = signature_update if touch_sig else (None, None)
    with conn.cursor() as cur:
        cur.execute(_UPDATE, (
            share_mode, display_name, description, list(tags or []),
            max_runtime_ms, price_cents, currency, attachments_allowed,
            returns_attachments, requires_human_auth,
            _jsonb_or_null(examples),
            _jsonb_or_null(runtime_profile),
            _jsonb_or_null(runtime_status),
            touch_sig, new_sig, touch_sig, new_kid,
            str(peer_id), name,
        ))
        row = cur.fetchone()
    return NOT_FOUND if row is None else _row_to_skill(row)


def soft_delete(conn, peer_id, name) -> int:
    """Return the number of rows affected across all versions."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE skills SET deleted_at = NOW() "
            "WHERE peer_id = %s AND name = %s AND deleted_at IS NULL",
            (str(peer_id), name))
        return cur.rowcount
