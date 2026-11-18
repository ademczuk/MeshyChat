"""The file surface's error codes and status-code pairings.

`ApiError` is IMPORTED from `apps.peers.errors`, not redefined. One exception class means
one `except` in the view layer; two classes with the same shape is how a handler ends up
catching one and 500-ing on the other. `apps/peers` is already built and is the house
example, so the dependency runs one way (files -> peers) and nothing here modifies it.

THE PAIRINGS ARE THE CONTRACT. These are the ones easiest to get wrong by one:

  E_FILE_NOT_FOUND       404   service-level "no row, or no access path"
  E_NOT_FOUND            404   route-level "calling peer not found"
  E_FILE_NOT_READY       409   NOT 425, NOT 404
  E_FILE_HASH_MISMATCH   422   also carries SizeMismatch
  E_FILE_SIZE_EXCEEDED   413
  E_FILE_IN_USE          409   same status as NOT_READY, different code
  E_BLOB_BACKEND_UNAVAILABLE 503 message is the CONSTANT "blob backend
                                                   error" - the detail is logged, never sent
  E_QUOTA_EXCEEDED       402   PAYMENT_REQUIRED, not 429
"""
from __future__ import annotations

import logging

# One class, imported rather than re-declared - see the module docstring.
from apps.peers.errors import (  # noqa: F401
    E_AUTH_INVALID,
    E_AUTH_MISSING,
    E_CONFLICT,
    E_FORBIDDEN,
    E_INTERNAL,
    E_NOT_FOUND,
    E_VALIDATION,
    ApiError,
    auth_invalid,
    auth_missing,
    conflict,
    forbidden,
    internal,
    not_found,
    validation,
)

log = logging.getLogger("meshychat.files")

# Exact error-code strings.
E_FILE_NOT_FOUND = "E_FILE_NOT_FOUND"
E_FILE_NOT_READY = "E_FILE_NOT_READY"
E_FILE_HASH_MISMATCH = "E_FILE_HASH_MISMATCH"
E_FILE_SIZE_EXCEEDED = "E_FILE_SIZE_EXCEEDED"
E_FILE_ATTACHMENT_INVALID = "E_FILE_ATTACHMENT_INVALID"
E_FILE_IN_USE = "E_FILE_IN_USE"
E_BLOB_BACKEND_UNAVAILABLE = "E_BLOB_BACKEND_UNAVAILABLE"
E_QUOTA_EXCEEDED = "E_QUOTA_EXCEEDED"


def file_not_found() -> ApiError:
    """404 with the FILE-specific code.

    A stranger's read of somebody else's artifact lands here too: the service returns
    NotFound rather than Forbidden so the status code cannot be used to probe for
    existence."""
    return ApiError(404, E_FILE_NOT_FOUND, "file not found")


def file_not_ready(status: str) -> ApiError:
    """The observed status is IN the message (`file not ready:
    status=pending`), because a client waiting on finalize branches on it."""
    return ApiError(409, E_FILE_NOT_READY, f"file not ready: status={status}")


def file_bytes_not_uploaded() -> ApiError:
    """The pre-HEAD in the finalize route maps a missing object to
    409 E_FILE_NOT_READY with its own message: finalization can arrive before the upload,
    which is a normal client race rather than a server fault."""
    return ApiError(409, E_FILE_NOT_READY, "file bytes not uploaded yet")


def hash_mismatch() -> ApiError:
    """Return the file-hash mismatch response."""
    return ApiError(422, E_FILE_HASH_MISMATCH, "hash mismatch")


def size_mismatch(declared: int, observed: int) -> ApiError:
    """SizeMismatch shares E_FILE_HASH_MISMATCH/422 with the hash
    case. Two distinct service errors, one wire code: not a typo here."""
    return ApiError(422, E_FILE_HASH_MISMATCH,
                    f"size mismatch: declared={declared}, observed={observed}")


def size_exceeded(declared: int, max_bytes: int) -> ApiError:
    """Return the file-size-exceeded response."""
    return ApiError(413, E_FILE_SIZE_EXCEEDED,
                    f"size exceeded: declared={declared}, max={max_bytes}")


def file_in_use() -> ApiError:
    """Return the file-in-use response."""
    return ApiError(409, E_FILE_IN_USE, "file in use by non-terminal call(s)")


def blob_backend(detail: str) -> ApiError:
    """Log the detail and return a constant
    public message. Sending `detail` would leak filesystem paths and R2 endpoints, so it
    stays on the exception for logs exactly as `internal()` does."""
    log.warning("blob backend error: %s", detail)
    err = ApiError(503, E_BLOB_BACKEND_UNAVAILABLE, "blob backend error")
    err.detail = detail
    return err


def product_access_denied(capability: str) -> ApiError:
    """402 for a REVOKED ENTITLEMENT, which is not the same thing as a full quota.

    Reusing `quota_exceeded` here would have put "quota exceeded for file_bytes: 0 would
    exceed cap 0" on the wire - a sentence that is false in every part. The status is shared
    because both mean "you may not consume more"; the MESSAGE must not be, because an
    operator reading it will otherwise go looking at usage counters for a problem that lives
    in the entitlement row.
    """
    return ApiError(402, E_QUOTA_EXCEEDED,
                    f"product access denied while resolving {capability}")


def quota_exceeded(capability: str, actual: int, cap: int) -> ApiError:
    """Use the quota-exceeded message:
    `quota exceeded for {capability}: {actual} would exceed cap {cap}`."""
    return ApiError(402, E_QUOTA_EXCEEDED,
                    f"quota exceeded for {capability}: {actual} would exceed cap {cap}")


def upload_size_exceeds_token(actual: int, max_bytes: int) -> ApiError:
    """Return the raw PUT route's own 413."""
    return ApiError(413, E_FILE_SIZE_EXCEEDED,
                    f"upload size {actual} exceeds token max {max_bytes}")
