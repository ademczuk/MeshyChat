"""The retention job bodies (STATUS D10, D11, D12, D13, D15, D17).

Each body is what apps/jobs/registry.py registers: it takes the work connection (the
application role, under the job's statement_timeout, rule E7) and the Job, and deletes at
most one bounded batch (B5). Every delete is keyed on the row's own EXPIRY or on its age
(rule i), never on a live object's identity and never on a row still in force.

Where the delete already existed it is CALLED, not rewritten: the files janitor
(`apps/files/service.FileService.sweep_*`, D13) and the usage prune
(`apps/mesh/usage.prune`, D12). The auth tables' deletes live beside the code that writes
them (apps/authapi/retention.py), the flow nonces' beside the envelope nonces'
(apps/calls/nonce_ledger.py). Only the DPoP ledger's delete is here, because its two
writers (apps/peers/auth.py, apps/wsgw/db.py) are request paths with no module of their
own for the ledger.

The shared loop (`manage.py run_jobs --loop`) ticks every job; the test server runs it
every 30 s against meshy_nodetest, so a sweep that deleted a live row would show there.
"""
from __future__ import annotations

import logging

from apps.authapi import retention as auth_retention
from apps.calls import nonce_ledger
from apps.core import rls
from apps.files import blob as blobmod
from apps.files import config as files_config
from apps.files import service as files_service
from apps.jobs.result import TickResult
from apps.mesh import usage
from apps.wsgw.constants import DPOP_WINDOW_SECS

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ D10 dpop_jti_seen

#: Kept past `expires_at` before a spent jti is deleted. `expires_at` is the proof's
#: timestamp plus DPOP_WINDOW_SECS, so it is exactly the end of acceptability, judged on
#: the APP clock by verify_dpop, while this sweep reads the DATABASE clock. With no margin
#: a database clock ahead of the app's deletes the row while the proof is still
#: acceptable, and a replay then spends it afresh: the finding divergence 036 recorded for
#: the envelope ledger, whose answer there was a second window at write time. Here the two
#: writers are request paths, so the second window is taken at sweep time instead.
DPOP_SWEEP_MARGIN_SECS = DPOP_WINDOW_SECS


def sweep_expired_dpop_jtis(conn, *, limit: int) -> int:
    """Delete up to `limit` spent DPoP jtis more than a window past `expires_at`, oldest
    first, on `dpop_jti_seen_expires_idx` (sql/046). The table has no row-level security
    (measured, apps/wsgw/db.py). Returns rows deleted."""
    if limit <= 0:
        raise ValueError("limit must be > 0; a zero bound would be a silent no-op")
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM dpop_jti_seen WHERE jti IN ("
            "  SELECT jti FROM dpop_jti_seen "
            "   WHERE expires_at < now() - make_interval(secs => %s) "
            "   ORDER BY expires_at LIMIT %s)",
            (DPOP_SWEEP_MARGIN_SECS, int(limit)))
        return cur.rowcount


def run_dpop_jti_sweep(conn, job) -> int:
    return sweep_expired_dpop_jtis(conn, limit=job.max_batch)


# ------------------------------------------------- D11, D15: auth tables; D17 flow nonces

def run_pairing_code_sweep(conn, job) -> int:
    return auth_retention.sweep_expired_pairing_codes(conn, limit=job.max_batch)


def run_auth_challenge_sweep(conn, job) -> int:
    return auth_retention.sweep_expired_auth_challenges(conn, limit=job.max_batch)


def run_owner_session_sweep(conn, job) -> int:
    return auth_retention.sweep_expired_owner_sessions(conn, limit=job.max_batch)


def run_flow_nonce_sweep(conn, job) -> int:
    return nonce_ledger.sweep_expired_flow_nonces(conn, limit=job.max_batch)


# --------------------------------------------------------------- D12 mesh_route_usage

_OLDEST_AGE = ("SELECT (now() AT TIME ZONE 'UTC')::date - min(day) FROM mesh_route_usage")


def _oldest_day_age(conn) -> int | None:
    """Days between today (UTC) and the oldest row's day, read on the day index. The
    table is FORCE-RLS, readable only under server_internal (sql_meshychat/005)."""
    with rls.server_internal(conn):
        with conn.cursor() as cur:
            cur.execute(_OLDEST_AGE)
            return cur.fetchone()[0]


def run_mesh_route_usage_retention(conn, job) -> TickResult:
    """`apps/mesh/usage.prune`, the 90-day retention sql_meshychat/005 states, ONE UTC day
    a tick: the oldest day, and only when it is at least RETENTION_DAYS old.

    `prune(keep_days=k)` deletes `day <= today - k`; with k the oldest row's age that is
    exactly the oldest day. The prune has no row bound of its own and is not edited here,
    so the bound is the day: a first run over a long backlog is one small delete per tick
    (routes x peers x outcomes rows, ~450 a day at fleet size) instead of one delete of
    everything, and `more_work_pending` brings the next day forward (STATUS D18).
    """
    age = _oldest_day_age(conn)
    if age is None or age < usage.RETENTION_DAYS:
        return TickResult(rows=0, more_work_pending=False)
    rows = usage.prune(conn, keep_days=age)
    after = _oldest_day_age(conn)
    return TickResult(rows=rows, more_work_pending=(
        after is not None and after >= usage.RETENTION_DAYS))


# --------------------------------------------------------------------- D13 files janitor

#: Rows per sweep statement: the `LIMIT 100` in apps/files/repository.py's
#: `sweep_pending_past_window` and `sweep_ready_past_grace`.
#: A test holds the two equal.
FILE_SWEEP_CAP = 100


def _unswept_file_rows(conn) -> int:
    """Rows the janitor WOULD sweep had it a store, at most FILE_SWEEP_CAP + 1 read: pending
    past the upload window, ready past grace. Without a backend the window is not validated
    at startup, so a value that does not parse is the default here, never a failed tick.
    The ready half omits the sweep's in-flight-call re-check: it counts rows past grace,
    which is what an operator reading the warning needs, not what a sweep would delete."""
    try:
        window = files_config._int_env("MESHYCHAT_UPLOAD_WINDOW_SECS",
                                       files_config.DEFAULT_UPLOAD_WINDOW_SECS)
    except files_config.ConfigError:
        window = files_config.DEFAULT_UPLOAD_WINDOW_SECS
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM (SELECT 1 FROM file_artifacts "
            " WHERE (status = 'pending' "
            "        AND created_at < now() - make_interval(secs => %s::float8)) "
            "    OR (status = 'ready' AND grace_until IS NOT NULL AND grace_until < now()) "
            " LIMIT %s) eligible",
            (float(window), FILE_SWEEP_CAP + 1))
        return cur.fetchone()[0]


def run_file_janitor(conn, job) -> TickResult:
    """Every 5 s: `pending`
    past the upload window becomes `failed` and loses its object; `ready` past its grace
    is deleted with its object. Both statements, one bounded batch each, per tick.

    Without a blob backend (MESHYCHAT_BLOB_BACKEND unset at startup) the janitor does nothing
    and succeeds: no file route serves, so nothing creates an artifact, and marking a row
    `failed` or deleting it without deleting its object would orphan the object. But the
    jobs process can lack the backend while the web process has it (their environments
    differ); then rows past their window pile up under `ok rows=0`, so while any exist
    the tick says so in a warning, counts only.
    """
    try:
        cfg, store = blobmod.active()
    except blobmod.BlobError:
        unswept = _unswept_file_rows(conn)
        if unswept:
            log.warning(
                "file janitor: no blob backend in this process (MESHYCHAT_BLOB_BACKEND unset), "
                "and %d%s file row(s) are past their window; nothing is swept until the "
                "jobs process has the web process's blob settings",
                min(unswept, FILE_SWEEP_CAP), "+" if unswept > FILE_SWEEP_CAP else "")
        return TickResult(rows=0, more_work_pending=False)
    svc = files_service.FileService(conn, store, cfg)
    pending = svc.sweep_pending_expired(cfg.upload_window_secs)
    ready = svc.sweep_ready_grace_expired()
    counts = (len(pending["swept_ids"]), len(ready["swept_ids"]))
    failures = pending["blob_delete_failures"] + ready["blob_delete_failures"]
    if failures:
        # The service logged each one; this line is the tick's total, so a janitor that
        # orphans objects on every tick is visible in one place.
        log.warning("file janitor: %d object delete(s) failed this tick", failures)
    return TickResult(rows=sum(counts),
                      more_work_pending=any(n >= FILE_SWEEP_CAP for n in counts))
