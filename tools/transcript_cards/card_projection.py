"""Bounded local card projection; native IDs deduplicate across source files.

Only owner-approved card.v1 bytes enter here. Immutable batches remain replay
evidence; consumers must use this merged projection rather than replace a card
with each raw batch. No server receipt, upload, pruning or full-dialogue claim.
Legacy stores without complete projection coverage require explicit migration.
"""
from dataclasses import dataclass
import hashlib
import json
import re

import incremental_store as ist


@dataclass(frozen=True)
class Limits:
    cards: int = 4096
    rows_per_batch: int = 1024
    card_bytes: int = 256 * 1024

    def __post_init__(self):
        if any(type(v) is not int or v <= 0 for v in vars(self).values()):
            raise ist.StoreError("positive projection limits required")


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def card_key(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise ist.StoreError("approved card identity required")
    return value


def load(data, digest):
    if hashlib.sha256(data).hexdigest() != digest:
        raise ist.StoreConflict("projected card digest disagreement")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ist.StoreConflict("projected card must be an object")
    return value


def merge_source(card):
    """Validate only the approved fields consumed by projection merging.

    Fixed reasons contain no card text, paths or rejected field values. This
    is not a replacement for the owner's full card.v1 approval policy.
    """
    if not isinstance(card, dict) or not isinstance(card.get("time"), str) or not card["time"]:
        raise ist.StoreError("projection card requires record time")
    source = card.get("source")
    if not isinstance(source, dict):
        raise ist.StoreError("projection card requires source object")
    digest = source.get("source_sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ist.StoreError("projection card requires source digest")
    for field in ("files", "sessions", "other_content"):
        values = source.get(field, [] if field == "other_content" else None)
        if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
            raise ist.StoreError("projection card requires provenance lists")
    return source


def merge(first, second):
    """Earlier record time/hash wins; all approved provenance/hashes survive."""
    a, b = merge_source(first), merge_source(second)
    selected = first if (first["time"], a["source_sha256"]) <= (
        second["time"], b["source_sha256"]) else second
    result = dict(selected, source=dict(selected["source"]))
    for field in ("files", "sessions"):
        result["source"][field] = sorted(set(a[field]) | set(b[field]))
    hashes = {a["source_sha256"], b["source_sha256"]}
    hashes.update(a.get("other_content", []))
    hashes.update(b.get("other_content", []))
    hashes.discard(result["source"]["source_sha256"])
    if hashes:
        result["source"]["other_content"] = sorted(hashes)
    return result


class Projection:
    def __init__(self, seat, parser, limits=None):
        if not isinstance(seat, str) or not re.fullmatch(r"[a-z0-9_-]{1,80}", seat):
            raise ist.StoreError("bounded seat required")
        ist._parser(parser)
        if limits is not None and not isinstance(limits, Limits):
            raise ist.StoreError("validated projection limits required")
        self.seat, self.parser, self.limits = seat, parser, limits or Limits()

    @staticmethod
    def schema(db):
        db.execute("""CREATE TABLE IF NOT EXISTS card_projection (
            seat TEXT NOT NULL, parser TEXT NOT NULL, id TEXT NOT NULL,
            batch TEXT NOT NULL, digest TEXT NOT NULL, data BLOB NOT NULL,
            blocked INTEGER NOT NULL, PRIMARY KEY(seat,parser,id))""")
        db.execute("CREATE TABLE IF NOT EXISTS card_projection_batches (key TEXT PRIMARY KEY)")

    @staticmethod
    def coverage(db, pending=None):
        missing = db.execute("""SELECT b.key FROM batches b LEFT JOIN card_projection_batches p
            ON b.key=p.key WHERE p.key IS NULL LIMIT 2""").fetchall()
        orphan = db.execute("""SELECT 1 FROM card_projection_batches p LEFT JOIN batches b
            ON p.key=b.key WHERE b.key IS NULL LIMIT 1""").fetchone()
        if orphan or missing != ([(pending,)] if pending is not None else []):
            raise ist.StoreConflict("projection coverage incomplete; explicit migration required")

    @ist._private_errors
    def verify(self, store):
        """Refuse legacy/mixed coverage even when this wake has no new output."""
        with store._connection() as db:
            db.execute("BEGIN")
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='card_projection_batches'").fetchone():
                if db.execute("SELECT 1 FROM batches LIMIT 1").fetchone():
                    raise ist.StoreConflict("projection coverage incomplete; explicit migration required")
                return  # A new empty store needs no implicit history migration.
            self.coverage(db)

    @ist._private_errors
    def capacity(self, store):
        """Content-free status of the store-wide lifetime identity budget.

        No retention/pruning or remote receipt is inferred. All local blocked
        IDs require server-cleanup reconciliation before graph safety can be
        claimed; this store has no evidence whether they were uploaded.
        """
        with store._connection() as db:
            db.execute("BEGIN")
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='card_projection_batches'").fetchone():
                self.coverage(db)
                count, blocked = db.execute("SELECT COUNT(*),COALESCE(SUM(blocked),0) FROM card_projection").fetchone()
            else:
                if db.execute("SELECT 1 FROM batches LIMIT 1").fetchone():
                    raise ist.StoreConflict("projection coverage incomplete; explicit migration required")
                count, blocked = 0, 0
        remaining = max(0, self.limits.cards - count)
        return {"scope": "store_lifetime_all_seats_and_parsers", "identities": count,
                "identity_limit": self.limits.cards, "remaining_identities": remaining,
                "capacity_state": "full" if not remaining else (
                    "near_limit" if count * 10 >= self.limits.cards * 9 else "available"),
                "existing_id_updates_allowed_at_identity_limit": True,
                "blocked_identities": blocked, "blocked_server_cleanup_unverified": blocked,
                "retention": "not_implemented", "server_cleanup": "unverified",
                "local_only": True}

    def __call__(self, db, batch, output, drops, *, replay=False):
        self.schema(db)
        parser = db.execute("SELECT parser FROM sources WHERE source=?", (batch.source,)).fetchone()
        if parser != (self.parser,):
            raise ist.StoreConflict("projection parser mismatch")
        if replay:
            self.coverage(db)
            if not db.execute("SELECT 1 FROM card_projection_batches WHERE key=?", (batch.key,)).fetchone():
                raise ist.StoreConflict("replayed projection missing")
            return
        self.coverage(db, pending=batch.key)
        rows = output.splitlines()
        rejected = drops.splitlines()
        if len(rows) + len(rejected) > self.limits.rows_per_batch:
            raise ist.StoreError("projection batch row limit exceeded")
        for row, blocked in [(r, False) for r in rows] + [(r, True) for r in rejected]:
            if len(row) > self.limits.card_bytes:
                raise ist.StoreError("projection card byte limit exceeded")
            card = json.loads(row)
            if not isinstance(card, dict):
                raise ist.StoreError("approved card object required")
            cid = card_key(card.get("id"))
            if not blocked and (card.get("schema") != "card.v1" or card.get("seat") != self.seat):
                raise ist.StoreError("approved card schema/seat mismatch")
            if not blocked:
                merge_source(card)  # Refuse malformed first cards as well as later variants.
            params = (self.seat, self.parser, cid)
            size = db.execute("SELECT length(data) FROM card_projection WHERE seat=? AND parser=? AND id=?",
                              params).fetchone()
            if size:
                if size[0] > self.limits.card_bytes:
                    raise ist.StoreError("retained projection byte limit exceeded")
                previous = db.execute("SELECT digest,data,blocked FROM card_projection WHERE seat=? AND parser=? AND id=?",
                                      params).fetchone()
                old = load(previous[1], previous[0])
                if previous[2]:
                    continue  # A detector hit cannot be undone by a later source.
                if not blocked:
                    card = merge(old, card)
            elif db.execute("SELECT COUNT(*) FROM card_projection").fetchone()[0] >= self.limits.cards:
                raise ist.StoreError("projection card count limit exceeded")
            # A drop has no source content hash, so preserve only the approved
            # detector row and block the whole ID. Never guess a safe variant.
            data = encoded(card)
            if len(data) > self.limits.card_bytes:
                raise ist.StoreError("merged provenance byte limit exceeded")
            db.execute("INSERT OR REPLACE INTO card_projection VALUES(?,?,?,?,?,?,?)",
                       (self.seat, self.parser, cid, batch.key,
                        hashlib.sha256(data).hexdigest(), data, int(blocked)))
        db.execute("INSERT INTO card_projection_batches VALUES(?)", (batch.key,))

    @ist._private_errors
    def read(self, store, cid):
        """One bounded private LOCAL card with its committing batch and digest."""
        card_key(cid)
        with store._connection() as db:
            db.execute("BEGIN")
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='card_projection_batches'").fetchone():
                raise ist.StoreConflict("projection not initialized; explicit migration required")
            self.coverage(db)
            params = (self.seat, self.parser, cid)
            size = db.execute("SELECT length(data) FROM card_projection WHERE seat=? AND parser=? AND id=?",
                              params).fetchone()
            if size is None:
                return None
            if size[0] > self.limits.card_bytes:
                raise ist.StoreError("retained projection byte limit exceeded")
            row = db.execute("SELECT batch,digest,data,blocked FROM card_projection WHERE seat=? AND parser=? AND id=?",
                             params).fetchone()
            card = load(row[2], row[1])
            store._committed(db, row[0])
            return {"card": None if row[3] else card, "blocked": bool(row[3]),
                    "drop": card if row[3] else None,
                    "batch": row[0], "sha256": row[1], "local_only": True}
