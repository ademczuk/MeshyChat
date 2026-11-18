"""Service layer: the 3-step upload, the access check, the download presign, delete,
and the lifecycle sweeps.

THE ACCESS CHECK IS THE WHOLE POINT OF THIS FILE (spec 8.3):

  1. SAME OWNER - the artifact's `owner_id` equals the requester's owner. ANY peer of
     that owner may read; it is an owner-level grant, not a peer-level one.
  2. CALL PARTICIPANT - the requesting peer is `caller_peer_id` or `target_peer_id` of
     at least one `skill_calls` row that references the artifact in `args_attachments`
     or `result_attachments`.
  2b. MESSAGE RECIPIENT (issue #118, for a DOWNLOAD and its metadata only, not for call admission) - a
     mail message that attached the file addressed the requesting peer in `to` or `cc`, and its sender
     could read the file by rule 1 or 2 when it was written (apps/files/grants.py; the grant is recorded
     at write time and withdrawn by a redaction). Every presigned URL is recorded as a pickup receipt.
  3. otherwise NOT FOUND, never FORBIDDEN. A 403 would confirm the artifact exists to a
     stranger who guessed its id; the 404 is a deliberate existence-leak defence
     and must not be "improved" into a 403 by a later reader.

DELETE is stricter than read: owner ONLY. A cross-owner call participant
can download an artifact and cannot delete it, and that asymmetry is intentional.

No `.count()`, no aggregate, no unbounded scan on any request path. The in-use guard is
a SQL `EXISTS` that stops at the first match; the participant check reads at most 100
referencing rows. The most expensive thing a single file request does is a primary-key
lookup plus one indexed `peers` read.
"""
from __future__ import annotations

import hashlib
import hmac
import uuid

from . import errors, quota, repository as repo
from . import grants
# BlobNotFound is deliberately NOT imported here. Finalize maps
# EVERY head error - including a missing object - to a backend error (`errors.blob_backend`) to avoid leaking
# missing state"; only the ROUTE special-cases it
# into 409 E_FILE_NOT_READY. `BlobError` is the base class, so catching it here keeps
# that distinction at the route boundary.
from .blob import BlobError, BlobStore, PresignedUrl

#: FINALIZE_BUF_SIZE is 64 KiB. The finalize hash is STREAMED, never
#: `read()` in one go: a 100 MiB default cap times a handful of concurrent finalizes is
#: how reading an entire object turns an integrity check into an OOM.
FINALIZE_BUF_SIZE = 64 * 1024


class FileService:
    """Holds the psycopg connection, the blob
    store and the blob config."""

    def __init__(self, conn, store: BlobStore, cfg):
        self.conn = conn
        self.store = store
        self.cfg = cfg

    # ------------------------------------------------------------------ helpers

    def resolve_owner_id(self, peer_id) -> uuid.UUID:
        """ONE indexed lookup on `peers(id) WHERE status='active'`.

        A deleted or suspended peer still holds an unexpired JWT, so the token is not the
        last word. The miss is the GENERIC 404
        E_NOT_FOUND, not E_FILE_NOT_FOUND - because the thing that is missing is the
        caller, not a file."""
        with self.conn.cursor() as cur:
            cur.execute("SELECT owner_id FROM peers WHERE id = %s AND status = 'active'",
                        (str(peer_id),))
            rec = cur.fetchone()
        if rec is None:
            raise errors.not_found()
        return rec[0]

    def authorize_read(self, file_id, requesting_peer, requesting_owner) -> repo.FileRow:
        """Returns the row, or raises the 404-shaped file error."""
        return self._authorize_download(file_id, requesting_peer, requesting_owner)[0]

    def _authorize_download(self, file_id, requesting_peer, requesting_owner):
        """(row, rule, granting message key) for the reader, or the 404-shaped file error. Rule 3 (issue #118,
        apps/files/grants.py) counts for a DOWNLOAD only, never for call admission (readable_row), so a peer a message
        addressed cannot pass the file on by attaching it to a call."""
        row, rule = readable_rule(self.conn, file_id, requesting_peer, requesting_owner)
        if row is not None:
            return row, rule, None
        row = repo.get(self.conn, file_id)
        key = None if isinstance(row, repo.Absent) else repo.message_grant_key(self.conn, file_id, requesting_peer)
        if key is None:
            raise errors.file_not_found()
        return row, grants.RULE_MESSAGE, key

    # ------------------------------------------------------------------- upload

    def create_artifact(self, owner_id, uploaded_by_peer_id, filename, content_type,
                        size_bytes_declared: int) -> dict:
        """Step 1: a `pending` row + a presigned PUT.

        The size check happens BEFORE the row and before the presign:
        the point is to refuse the URL up front rather than after the client has spent
        the bytes."""
        if size_bytes_declared > self.cfg.max_file_bytes:
            raise errors.size_exceeded(size_bytes_declared, self.cfg.max_file_bytes)

        file_id = uuid.uuid4()
        # The key shape is what makes
        # `file_artifacts_r2_key_key` (UNIQUE) a per-artifact constraint rather than a
        # collision waiting to happen, and it is what `safe_path` expects to see.
        r2_key = f"{owner_id}/{file_id}"

        repo.insert_with_id(self.conn, file_id, owner_id, uploaded_by_peer_id, r2_key,
                            content_type, filename)

        try:
            presigned: PresignedUrl = self.store.presign_put(
                r2_key, content_type, size_bytes_declared, self.cfg.presign_ttl_secs)
        except BlobError as exc:
            raise errors.blob_backend(str(exc))

        row = repo.get(self.conn, file_id)
        if isinstance(row, repo.Absent):
            # The row was inserted a statement ago; its absence is a
            # real fault, not a 404 for the client to interpret.
            raise errors.file_not_found()

        return {"artifact": row, "upload_url": presigned.url,
                "upload_headers": presigned.headers, "expires_at": presigned.expires_at}

    def finalize(self, file_id, client_sha256: str) -> repo.FileRow:
        """Step 3: verify the bytes the client actually uploaded.

        Order matters and each step has its own failure code:
          row missing            -> 404 E_FILE_NOT_FOUND
          status != pending      -> 409 E_FILE_NOT_READY (the OBSERVED status is echoed)
          backend HEAD fails     -> 503 E_BLOB_BACKEND_UNAVAILABLE (never 404: a missing
                                    object here must not be reported as a missing row)
          head.size > max        -> row failed + object deleted + 413
          sha mismatch           -> row failed + object deleted + 422
          streamed != head size  -> row failed + object deleted + 422 (backend metadata
                                    and body disagree)
        The hash comparison is constant-time - a byte-at-a-time compare
        against a server-recomputed digest is a real oracle, cheap to remove."""
        row = repo.get(self.conn, file_id)
        if isinstance(row, repo.Absent):
            raise errors.file_not_found()
        if row.status != repo.STATUS_PENDING:
            raise errors.file_not_ready(row.status)

        try:
            meta = self.store.head(row.r2_key)
        except BlobError as exc:
            raise errors.blob_backend(f"head: {exc}")

        if meta.size_bytes > self.cfg.max_file_bytes:
            self._fail_and_purge(file_id, row.r2_key)
            raise errors.size_exceeded(meta.size_bytes, self.cfg.max_file_bytes)

        try:
            digest = hashlib.sha256()
            total = 0
            with self.store.get_stream(row.r2_key) as stream:
                while True:
                    chunk = stream.read(FINALIZE_BUF_SIZE)
                    if not chunk:
                        break
                    digest.update(chunk)
                    total += len(chunk)
        except BlobError as exc:
            raise errors.blob_backend(f"get_stream: {exc}")
        server_sha = digest.hexdigest()

        if not hmac.compare_digest(server_sha, str(client_sha256)):
            self._fail_and_purge(file_id, row.r2_key)
            raise errors.hash_mismatch()

        if total != meta.size_bytes:
            self._fail_and_purge(file_id, row.r2_key)
            raise errors.size_mismatch(meta.size_bytes, total)

        if not repo.mark_ready(self.conn, file_id, server_sha, total):
            # Lost a concurrent finalize race. Re-read and
            # disambiguate rather than propagating the raw miss as a misleading 404.
            again = repo.get(self.conn, file_id)
            if isinstance(again, repo.Absent):
                raise errors.file_not_found()
            if again.status == repo.STATUS_READY:
                return again          # idempotent: the winner verified the same bytes
            raise errors.file_not_ready(again.status)

        final = repo.get(self.conn, file_id)
        if isinstance(final, repo.Absent):
            raise errors.file_not_found()
        return final

    def _fail_and_purge(self, file_id, r2_key) -> None:
        """Both cleanup operations are best effort, and
        neither may mask the error that is about to be raised."""
        try:
            repo.mark_failed(self.conn, file_id)
        except Exception:                                  # noqa: BLE001 - see docstring
            errors.log.warning("mark_failed(%s) failed while handling a finalize error",
                               file_id, exc_info=True)
        try:
            self.store.delete(r2_key)
        except BlobError:
            errors.log.warning("blob delete for %s failed after a finalize error; "
                               "orphan possible", r2_key, exc_info=True)

    # ----------------------------------------------------------------- download

    def get_metadata(self, file_id, requesting_peer, requesting_owner) -> repo.FileRow:
        """Same access check as the download; NO `status='ready'` guard,
        deliberately - a caller polls this to watch a pending upload become ready."""
        return self.authorize_read(file_id, requesting_peer, requesting_owner)

    def presign_download(self, file_id, requesting_peer, requesting_owner) -> PresignedUrl:
        """Access check, then a `ready`-only status guard."""
        row, rule, granted_by = self._authorize_download(file_id, requesting_peer, requesting_owner)
        if row.status != repo.STATUS_READY:
            raise errors.file_not_ready(row.status)
        try:
            url = self.store.presign_get(row.r2_key, self.cfg.presign_ttl_secs)
        except BlobError as exc:
            raise errors.blob_backend(f"presign_get: {exc}")
        # Issue #118: the pickup receipt. A URL was ISSUED; the server never sees the GET, so this is not "delivered".
        # Written before the URL is returned and NOT caught: a failed insert fails the presign, so "no receipt" always
        # means "no URL was handed out" (memory-bxxxxx, review of the design). A retry within 60 s writes no second row.
        repo.record_download(self.conn, file_id, requesting_peer, rule, row.sha256, granted_by)
        return url

    def grant(self, file_id, label, requesting_peer, requesting_owner) -> list[str]:
        """POST /v1/files/{id}/grants: the file's OWNER (rule 1) lets the peers `label` reaches download it (rule 3).
        Anyone else, a reader admitted by rule 2 or 3 included, gets the same 404 as a missing file, so a reader can
        never pass a file on and nobody learns a file exists by trying to grant it."""
        row = repo.get(self.conn, file_id)
        if isinstance(row, repo.Absent) or str(row.owner_id) != str(requesting_owner):
            raise errors.file_not_found()
        return repo.record_direct_grant(self.conn, file_id, label, requesting_peer)

    def list_downloads(self, file_id, requesting_peer, requesting_owner) -> list[dict]:
        """Issue #118: the receipts of one file, newest 100, for peers of the file's OWNER only (rule 1, which the
        sender of a message normally is). Anyone else, a reader admitted by rule 2 or 3 included, gets the 404."""
        row = repo.get(self.conn, file_id)
        if isinstance(row, repo.Absent) or str(row.owner_id) != str(requesting_owner):
            raise errors.file_not_found()
        return repo.list_downloads(self.conn, file_id, 100)

    # ------------------------------------------------------------------- delete

    def soft_delete(self, file_id, requesting_peer, requesting_owner) -> None:
        """Owner-only, in-use-guarded, then row + object.

        Despite the name, this hard-deletes the row.
        Blob-delete failure is logged and does NOT fail the request: the row is already
        gone, and failing the request cannot restore it; an orphaned blob is recorded for
        operators to clean up."""
        row = repo.get(self.conn, file_id)
        if isinstance(row, repo.Absent):
            raise errors.file_not_found()
        if str(row.owner_id) != str(requesting_owner):
            # Existence-leak-safe: the cross-owner case is a 404, not a 403, even though
            # the server knows the row is there.
            raise errors.file_not_found()

        if repo.has_non_terminal_reference(self.conn, file_id):
            raise errors.file_in_use()

        if not repo.delete(self.conn, file_id):
            raise errors.file_not_found()
        try:
            self.store.delete(row.r2_key)
        except BlobError:
            errors.log.warning("blob delete failed after row delete for %s; orphan "
                               "possible", row.r2_key, exc_info=True)

    # --------------------------------------------------------- lifecycle (spec 11)

    def sweep_pending_expired(self, window_secs: int) -> dict:
        """`pending` past the upload window -> `failed` + blob delete.
        Capped at 100 rows per call, by the repository statement."""
        rows = repo.sweep_pending_past_window(self.conn, window_secs)
        return self._sweep_blobs(rows)

    def sweep_ready_grace_expired(self) -> dict:
        """`ready` past `grace_until` -> row and object gone."""
        rows = repo.sweep_ready_past_grace(self.conn)
        return self._sweep_blobs(rows)

    def _sweep_blobs(self, rows) -> dict:
        if isinstance(rows, repo.Absent):
            return {"swept_ids": [], "blob_delete_failures": 0}
        swept, failures = [], 0
        for file_id, r2_key in rows:
            swept.append(file_id)
            try:
                self.store.delete(r2_key)
            except BlobError:
                failures += 1
                errors.log.warning("sweep: blob delete failed for %s; orphan possible",
                                   r2_key, exc_info=True)
        return {"swept_ids": swept, "blob_delete_failures": failures}


def readable_row(conn, file_id, requesting_peer, requesting_owner) -> repo.FileRow | None:
    """Rules 1 and 2 (readable_rule). Call admission's check: rule 3 is deliberately not part of it."""
    return readable_rule(conn, file_id, requesting_peer, requesting_owner)[0]


def readable_rule(conn, file_id, requesting_peer, requesting_owner) -> tuple[repo.FileRow | None, int]:
    """The access check of the module docstring (rules 1 and 2): the row when the peer may read it, else None.

    None for a missing row and for a row the peer may not read alike, so no caller can tell the two
    apart. Two callers: `FileService.authorize_read` (a download) and call admission
    (`apps/calls/pipeline.py`, issue #122), which refuses a call whose `args_attachments` names a file
    its caller could not already download. Without that, rule 2 let a reference in a call MINT read
    access: attach any file id, and caller and target could both download it."""
    row = repo.get(conn, file_id)
    if isinstance(row, repo.Absent):
        return None, 0

    if str(row.owner_id) == str(requesting_owner):
        return row, grants.RULE_OWNER

    refs = repo.find_referencing_calls(conn, file_id)
    if isinstance(refs, repo.Absent):
        # `Empty`, not `[]`: nobody references the artifact, so path 2 cannot apply.
        return None, 0
    peer = str(requesting_peer)
    for ref in refs:
        if (ref.caller_peer_id is not None and str(ref.caller_peer_id) == peer) \
                or str(ref.target_peer_id) == peer:
            return row, grants.RULE_CALL
    return None, 0


def set_grace_until_for_terminal_call(conn, call_id: str, retention_secs: int) -> int:
    """When a call reaches a terminal status, start the grace window
    on every artifact it referenced. A file another non-terminal call still holds is
    DEFERRED by `set_grace_until`'s own `NOT EXISTS`, not skipped here."""
    found = repo.attachments_of_call(conn, call_id)
    if isinstance(found, repo.Absent):
        return 0
    args_attachments, result_attachments = found
    started = 0
    for file_id in list(args_attachments or []) + list(result_attachments or []):
        try:
            if repo.set_grace_until(conn, file_id, retention_secs):
                started += 1
        except Exception:                                  # noqa: BLE001 - best effort
            errors.log.warning("set_grace_until failed for %s (call %s); continuing",
                               file_id, call_id, exc_info=True)
    return started


# ----------------------------------------------------------------- quota (402)

def reserve_file_bytes(conn, owner_id, amount: int) -> quota.Reservation:
    """ATOMIC reserve-then-finalize. A plain read-then-bump check holds no lock, so two
    concurrent finalizes for one owner could both pass it before either bumped usage,
    pushing bytes used past the cap by an arbitrary amount."""
    service = quota.get_quota_service()
    try:
        return service.try_reserve(conn, owner_id, quota.CAPABILITY_FILE_BYTES, amount)
    except quota.QuotaExceeded as exc:
        raise errors.quota_exceeded(exc.capability, exc.actual, exc.cap)
    except quota.UnknownCapability as exc:
        raise errors.validation(f"unknown capability: {exc}")
    except quota.ProductAccessDenied as exc:
        # 402, the same status this surface already uses for "you may not consume more".
        # It must be MAPPED rather than left to escape: an unmapped raise would reach the
        # JSON-envelope middleware as a 500, and a revoked entitlement is a decision about
        # the caller, not a server fault. `capability` is named so the refusal says WHICH
        # cap was being resolved when the gate denied.
        raise errors.product_access_denied(quota.CAPABILITY_FILE_BYTES) from exc
