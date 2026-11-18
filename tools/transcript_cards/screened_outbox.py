"""Local schema/privacy preflight before freezing a complete wire batch.

The validator is a trusted application dependency, never transcript input.
This does not implement server grants, write-once admission or source identity.
No screen refusal is automatically converted into a permanent omission.
"""
import importlib
import json

from frozen_outbox import StoreError, _records


NAMESPACE = "shared/transcript-context"
UNAVAILABLE = "E_CONTEXT_SCREEN_UNAVAILABLE"
LOCAL_POLICY = "E_CONTEXT_LOCAL_POLICY"
REFUSALS = frozenset({
    "E_CONTEXT_KEY", "E_CONTEXT_SCHEMA", "E_CONTEXT_TIME",
    "E_CONTEXT_DIGEST", "E_CONTEXT_TOO_LARGE", "E_CONTEXT_TEXT_ENCODING",
    "E_CONTEXT_SEAT_MISMATCH", "E_CONTEXT_MANIFEST",
    "E_CONTEXT_MANIFEST_ORDER", "E_CONTEXT_WRITE_ONCE",
    "E_CONTEXT_REDACT_UNAVAILABLE", "E_CONTEXT_MANIFEST_PREDECESSOR",
    "E_CONTEXT_MANIFEST_ITEM_MISSING",
    "E_CONTEXT_SECRET", "E_CONTEXT_CLIENT_NAME", UNAVAILABLE, LOCAL_POLICY,
})


class AdmissionError(StoreError):
    """Bounded, content-free refusal; the original batch remains uncommitted."""

    def __init__(self, code):
        self.code = code if type(code) is str and code in REFUSALS else LOCAL_POLICY
        super().__init__(self.code + ": local context preflight refused the batch")


def freeze_screened(outbox, expected, safe_offset, items, manifest, checkpoint,
                    *, seat, validator=None, now=None, boundary=None):
    """Validate/screen ALL records before any output or parser-position commit.

    By default use apps.context.validation from the installed application.
    Offline compatibility checks can supply an explicitly hash-pinned canonical
    module. Missing policy, unavailable required screens and unexpected failures
    all fail closed. A retry must re-run preflight; no admission is cached.
    """
    records, _, _ = _records(items, manifest)
    values = [json.loads(record.value) for record in records]
    if type(seat) is not str or any(value["seat"] != seat for value in values):
        raise AdmissionError("E_CONTEXT_SEAT_MISMATCH")
    try:
        if validator is None:
            try:
                validator = importlib.import_module("apps.context.validation")
            except ImportError:
                raise AdmissionError(UNAVAILABLE) from None
        for record, value in zip(records, values):
            kind = validator.validate_context(NAMESPACE, record.key, value, now=now)
            # The pure validator returns None for an unconfigured namespace.
            if kind != value["kind"]:
                raise AdmissionError(UNAVAILABLE if kind is None else LOCAL_POLICY)
        counts = dict(written=0, omitted_secret=0, omitted_client_name=0,
                      omitted_too_large=0)
        # The F3 classes, as the server tallies them (apps/context/validation._enforce_manifest): present only in
        # a batch that holds an F3 item, then counted from the stored items (nxxxxx: without them every
        # F3 manifest failed here, so no F3 batch could ever be frozen).
        f3 = any(value["item"] in ("tool_exchange", "thinking") for value in values[:-1])
        if f3:
            counts.update(tool_exchange=0, thinking=0, call_missing=0, result_missing=0, parts_extra=0)
        for value in values[:-1]:
            name = "omitted_" + value["omitted_reason"] if value["item"] == "omitted" else "written"
            counts[name] += 1
            if value["item"] in ("tool_exchange", "thinking"):
                counts[value["item"]] += 1
            if value["item"] == "tool_exchange":
                counts["call_missing"] += value.get("call_status") == "missing"
                counts["result_missing"] += value.get("result_status") == "missing"
                counts["parts_extra"] += value.get("part", 1) > 1
        if counts != values[-1]["counts"]:
            raise AdmissionError("E_CONTEXT_MANIFEST")
        # The server checks every split exchange whole at the manifest; so does the preflight, where the validator
        # can (a validator before F3 has no such check, and admits no F3 kind either).
        if f3 and hasattr(validator, "_check_split_exchanges"):
            validator._check_split_exchanges(
                NAMESPACE, [(value["ordinal"], value) for value in values[:-1] if value["kind"] == "item"])
        # Follow the canonical namespace policy. Older pinned validators have no
        # switch and retain their original, conservative two-screen behavior.
        screens_apply = True
        if hasattr(validator, "screens_apply"):
            screens_apply = validator.screens_apply(NAMESPACE)
            if type(screens_apply) is not bool:
                raise AdmissionError(LOCAL_POLICY)
        if screens_apply:
            for value in values[:-1]:
                if value["item"] == "tool_exchange" and hasattr(validator, "_exchange_screen_text"):
                    # What the server screens for an exchange: the stored text, unescaped, and the decoded fields.
                    validator.screens.screen(*validator._exchange_screen_text(value))
                    continue
                validator.screens.screen(*[value[field] for field in validator.SCREENED_FIELDS
                                          if isinstance(value.get(field), str)])
    except AdmissionError:
        raise
    except Exception as error:
        # Never retain policy exception text, paths, names or matched material.
        raise AdmissionError(getattr(error, "code", None)) from None
    return outbox.freeze(expected, safe_offset, items, manifest, checkpoint,
                         boundary=boundary)
