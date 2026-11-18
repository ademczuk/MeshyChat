"""Bounded LOCAL staging through the owner's parser. No network or graph receipts.

One selected file per pending job per pass; fixed wake targets, private atomic
parser/output commits, and durable round-robin progress. A distinct spool is
required; source replacement/truncation or parser upgrade needs explicit review.
Append-only enrolled files are assumed: old prefix rewrites cannot be proved by
bounded window checks. Only Claude and interactive Codex are enrolled here.
"""
import contextlib
import hashlib
import json
from pathlib import Path
import tempfile

import capture_queue as queue
import incremental_store as ist
import native_window as nw
import transcript_cards as loader


def opaque(value):
    return hashlib.sha256(queue.canonical(value).encode()).hexdigest()


def parser_id():
    # Refuse stores captured before durable naming and built-in machine filtering.
    return f"transcript_cards:{loader.LOADER_VERSION}:{loader.ID_RULE}:{loader.CHECKPOINT_SCHEMA}:bounded-v3"


def dialect_budget(kind):
    """Owner-reviewed defaults; an explicit operator Budget overrides these."""
    record = {"claude": 4 * 1024 * 1024, "codex": 16 * 1024 * 1024}[kind]
    return nw.Budget(read_bytes=2 * record, record_bytes=record)


class EnrollmentPending(Exception):
    def __init__(self, window):
        self.window = window


class CodexPairBudgetError(queue.CaptureLimitError):
    """Codex must see an item and its event together to release the safe offset."""


def validate_budget(kind, budget):
    if not isinstance(budget, nw.Budget):
        raise nw.WindowError("validated window budget required")
    if kind == "codex" and (budget.records < 2 or budget.read_bytes < 2 * budget.record_bytes):
        raise CodexPairBudgetError("Codex requires records >= 2 and read_bytes >= 2 * record_bytes")


def validate_pending_observation(db, job, source, generation, window, budget):
    """Compare only equally scoped observations of the same pending record.

    Advancing the window start or increasing its budget can extend the observed
    prefix without changing the source. Never compare those different hashes.
    This check precedes parser/output commits; it is not whole-file immutability.
    """
    observed = window.blocked_record
    if not observed:
        return
    row = db.execute("SELECT state FROM incremental_pending WHERE job=? AND source=?",
                     (job, source)).fetchone()
    if not row:
        return
    prior = json.loads(row[0])
    comparable = (prior.get("generation") == generation
                  and prior.get("read_bytes") == budget.read_bytes
                  and prior.get("record_bytes") == budget.record_bytes
                  and all(prior.get(key) == observed.get(key)
                          for key in ("offset", "length", "scope", "record_complete")))
    if comparable and prior.get("sha256") != observed["sha256"]:
        raise nw.SourceChanged("pending source observation changed; review required")


def remember_window(db, job, source, generation, window, budget):
    """Persist blocked diagnostics per file; never raw bytes or local paths.

    A bounded prefix is explicitly NOT a full-record length/digest. Clearing one
    file's blockage cannot hide another file stalled in the same queue job.
    """
    with db:
        if window.blocked_record:
            state = {**window.blocked_record, "source": source, "generation": generation,
                     "reason": window.reason, "read_bytes": budget.read_bytes,
                     "record_bytes": budget.record_bytes}
            db.execute("INSERT OR REPLACE INTO incremental_pending VALUES(?,?,?)",
                       (job, source, queue.canonical(state)))
        else:
            db.execute("DELETE FROM incremental_pending WHERE job=? AND source=?", (job, source))


def targets(config, spool, limits):
    root, spool = Path(config["root"]), Path(spool).resolve()
    if root == spool or root in spool.parents or spool in root.parents:
        raise queue.CaptureLimitError("source and spool must be disjoint")
    if len(config["files"]) > limits.source_entries:
        raise queue.CaptureLimitError("selected source entry limit exceeded")
    found, identities = [], set()
    for name in config["files"]:
        _, info = queue.selected_file(root, name)
        identity = (info.st_dev, info.st_ino)
        if identity in identities:
            raise queue.CaptureLimitError("selected names refer to the same source file")
        identities.add(identity)
        found.append({"name": name, "device": info.st_dev, "inode": info.st_ino, "target": info.st_size})
    return found


def enroll(store, source, generation, parser, path, target, config, spool, db, job, limits, budget):
    """Feed a bounded FIRST-record snapshot to the owner's existing enrollment.

    loader.enroll reads readline without a byte cap; never point it at the live
    file. Raw scratch ownership is committed before bytes and cleaned on restart.
    This adapter interprets no native JSON or dialect itself.
    """
    identity = nw.Identity(target["device"], target["inode"])
    first = nw.read_window(path, identity, 0, target["target"],
                           nw.Budget(budget.read_bytes, budget.record_bytes, 1, budget.seconds))
    if not first.records:
        raise EnrollmentPending(first)
    queue.spool_inventory(spool, limits, reserve=len(first.records[0].data) + 65536)
    scratch = Path(tempfile.mkdtemp(dir=spool, prefix=".source-"))
    info = scratch.stat(follow_symlinks=False)
    owned = (scratch.name, str(info.st_dev), str(info.st_ino))
    with db:
        db.execute("INSERT INTO scratch(name,device,inode,job) VALUES(?,?,?,?)", (*owned, job))
    try:
        queue.private_scratch(scratch)
        frozen = scratch / Path(target["name"]).name
        frozen.write_bytes(first.records[0].data)
        dialect, meta = loader.enroll(frozen, kimi=False, antigravity=False)
        if dialect != config["kind"] or (dialect == "codex" and not loader._codex_interactive(frozen)):
            raise queue.CaptureLimitError("supported interactive source enrollment required")
        return store.enroll(source, generation, parser, {"dialect": dialect, "meta": meta},
                            loader.new_checkpoint(dialect, meta))
    finally:
        queue.remove_scratch(spool, db, *owned, limits)


def stage_incremental(spool, *, limits=None, budget=None, boundary=None):
    """One bounded pass; boundary is a synthetic kill/concurrency test seam.

    Queue completion follows store commit. A crash between them resumes the
    store rather than recapturing old bytes. A new wake extends fixed targets
    without clearing semantic state. Partial tails/held pairs remain pending.
    No completion is claimed for pending output delivery or graph indexing.
    """
    spool = Path(spool)
    limits = limits or queue.Limits()
    results, failures = [], []
    with queue.worker_lock(spool):
        queue.private_scratch(spool)
        with contextlib.closing(queue.database(spool)) as db:
            queue.capture_mode(db, "incremental")
            if queue.recover_scratch(spool, db, limits):
                failures.append(queue.CaptureLimitError("scratch quarantined; review required"))
            with db:
                db.execute("""CREATE TABLE IF NOT EXISTS incremental_jobs (
                    job TEXT PRIMARY KEY, generation INTEGER NOT NULL, targets TEXT NOT NULL,
                    next_file INTEGER NOT NULL DEFAULT 0)""")
                db.execute("""CREATE TABLE IF NOT EXISTS incremental_pending (
                    job TEXT NOT NULL, source TEXT NOT NULL, state TEXT NOT NULL,
                    PRIMARY KEY(job,source))""")
            rows = db.execute("""SELECT id,source,requested FROM jobs WHERE requested>staged
                AND NOT EXISTS(SELECT 1 FROM scratch WHERE scratch.job=jobs.id AND review=1)
                ORDER BY attempts,id LIMIT ?""", (limits.jobs_per_pass,)).fetchall()
            for job, text, wake in rows:
                with db:
                    db.execute("UPDATE jobs SET attempts=attempts+1 WHERE id=?", (job,))
                try:
                    if hashlib.sha256(text.encode()).hexdigest() != job:
                        raise queue.CaptureLimitError("enrolled source identity requires review")
                    config = queue.source_config(json.loads(text))
                    selected_budget = budget if budget is not None else dialect_budget(config["kind"])
                    validate_budget(config["kind"], selected_budget)
                    queue.spool_inventory(spool, limits, reserve=selected_budget.read_bytes + limits.cards_bytes + 65536)
                    state = db.execute("SELECT generation,targets,next_file FROM incremental_jobs WHERE job=?", (job,)).fetchone()
                    current = targets(config, spool, limits)
                    if state:
                        prior = json.loads(state[1])
                        if ([(t["name"], t["device"], t["inode"]) for t in prior] !=
                                [(t["name"], t["device"], t["inode"]) for t in current]
                                or any(n["target"] < p["target"] for n, p in zip(current, prior))):
                            raise nw.SourceChanged("enrolled source replaced or truncated; review required")
                        selected = current if wake > state[0] else prior
                        cursor = state[2]
                    else:
                        selected, cursor = current, 0
                    with db:
                        db.execute("""INSERT INTO incremental_jobs VALUES(?,?,?,?)
                            ON CONFLICT(job) DO UPDATE SET generation=excluded.generation,targets=excluded.targets""",
                            (job, wake, queue.canonical(selected), cursor))
                        # Persist dispatch rotation before parsing/crashing.
                        db.execute("UPDATE incremental_jobs SET next_file=? WHERE job=?", ((cursor + 1) % len(selected), job))
                    target = selected[cursor % len(selected)]
                    path, _ = queue.selected_file(config["root"], target["name"])
                    source = opaque([job, target["name"]])
                    generation = opaque([target["device"], target["inode"]])
                    parser = parser_id()
                    store = ist.Store(spool / "incremental-store", ist.Limits(output_bytes=limits.cards_bytes))
                    try:
                        snap = store.resume(source, generation, parser)
                    except ist.StoreConflict:
                        # enroll rejects existing incompatible state; no auto reset.
                        try:
                            snap = enroll(store, source, generation, parser, path, target, config,
                                          spool, db, job, limits, selected_budget)
                        except EnrollmentPending as pending:
                            validate_pending_observation(db, job, source, generation,
                                                         pending.window, selected_budget)
                            remember_window(db, job, source, generation, pending.window, selected_budget)
                            with db:
                                db.execute("UPDATE jobs SET error=NULL WHERE id=?", (job,))
                            results.append({"job": job, "generation": wake, "phase": "pending",
                                            "safe_offset": 0, "target": target["target"],
                                            "reason": pending.window.reason,
                                            "server_persistence": "unverified", "index_visibility": "unverified"})
                            continue
                    cp = snap.parser_state()
                    window = nw.read_window(path, nw.Identity(target["device"], target["inode"]),
                                            snap.offset, target["target"], selected_budget)
                    validate_pending_observation(db, job, source, generation, window, selected_budget)
                    raws, stats, updated = loader.read_file([(r.offset, r.data) for r in window.records],
                                                           cp, seat=config["seat"])
                    if stats.get("bad_lines"):
                        raise queue.CaptureLimitError("malformed complete records require review")
                    # Match run()'s built-in operator-attribution boundary before
                    # redaction approval. This does not screen Context payloads.
                    raws = [raw for raw in raws if not loader.machine_prefix_of(
                        raw, loader.BUILTIN_MACHINE_PREFIXES)]
                    cards, drops = loader.approved_cards(raws, config["seat"], target["name"])
                    output = b"".join((queue.canonical(cards[k]) + "\n").encode() for k in sorted(cards))
                    rejected = b"".join((queue.canonical(drops[k]) + "\n").encode() for k in sorted(drops))
                    same_output = (updated == cp and snap.batch is not None
                                   and store.committed(snap.batch)[1:] == (output, rejected))
                    if (updated != cp or output or rejected) and not same_output:
                        store.commit(snap, updated["offset"], output, rejected, updated, boundary=boundary)
                    if boundary:
                        boundary("before_queue_commit")
                    complete = True
                    for item in selected:
                        try:
                            retained = store.resume(opaque([job, item["name"]]),
                                                    opaque([item["device"], item["inode"]]), parser)
                            complete = complete and retained.offset == item["target"]
                        except ist.StoreConflict:
                            complete = False
                    queue.spool_inventory(spool, limits)
                    remember_window(db, job, source, generation, window, selected_budget)
                    with db:
                        if complete:
                            db.execute("UPDATE jobs SET staged=?,batch=?,error=NULL WHERE id=?",
                                       (wake, "incremental-store", job))
                        else:
                            db.execute("UPDATE jobs SET error=NULL WHERE id=?", (job,))
                    results.append({"job": job, "generation": wake, "phase": "staged" if complete else "pending",
                                    "safe_offset": updated["offset"], "target": target["target"],
                                    "reason": "parser_hold" if window.reached_target and updated["offset"] < target["target"] else window.reason,
                                    "server_persistence": "unverified", "index_visibility": "unverified"})
                except Exception as exc:
                    with db:
                        db.execute("UPDATE jobs SET error=? WHERE id=?", (type(exc).__name__, job))
                    failures.append(exc)
    if failures:
        raise failures[0]
    return results
