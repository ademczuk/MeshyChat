"""A stand-in for `codex app-server --stdio`: just the experimental queue methods codex_front uses, on an in-memory
queue. argv: <mode> <initial-ids-json> <record-file>. Modes: ok | no_experimental | exit_after_add | reorder_fail_once
| no_id. The record file gets one JSON line per request, so a test can assert what was called."""
import json
import sys
import uuid

mode, initial, record = sys.argv[1], json.loads(sys.argv[2]), sys.argv[3]
# an initial entry "ID" or "ID|clientUserMessageId"
queue = [{"id": e.split("|")[0], "input": [{"type": "text", "text": e}],
          "clientUserMessageId": e.split("|")[1] if "|" in e else None} for e in initial]
reorder_failed = False


def out(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    try:
        req = json.loads(line)
    except ValueError:
        continue
    with open(record, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"method": req.get("method"), "params": req.get("params")}) + "\n")
    m, rid, p = req.get("method"), req.get("id"), req.get("params") or {}
    if rid is None:
        continue                                              # a notification ("initialized")
    if m == "initialize":
        if mode == "no_read_after_init":           # answers, then never reads stdin again (pxx 9709c8)
            out({"id": rid, "result": {"userAgent": "fake/0.167.0"}})
            import time
            time.sleep(8)
            sys.exit(0)
        if mode == "no_experimental":
            out({"id": rid, "error": {"code": -32600, "message": "experimental api not enabled"}})
        else:
            out({"id": rid, "result": {"userAgent": "fake/0.167.0"}})
    elif m == "thread/queue/add":
        new = {"id": str(uuid.uuid4()), "input": p.get("input"), "clientUserMessageId": p.get("clientUserMessageId")}
        queue.append(new)
        if mode == "exit_after_add":
            sys.exit(0)                                       # added, but never answered
        out({"id": rid, "result": {"queuedSubmission": {} if mode == "no_id" else new}})
    elif m == "thread/queue/list":
        shown = queue[:-1] if mode == "vanish" and queue else queue    # the newest item is gone when listed
        out({"id": rid, "result": {"data": shown, "nextCursor": None}})
    elif m == "thread/queue/reorder":
        ids = p.get("queuedSubmissionIds") or []
        if mode == "reorder_fail_once" and not reorder_failed:
            reorder_failed = True
            out({"id": rid, "error": {"code": -32600, "message": "queue reorder must include every queued submission exactly once"}})
        elif sorted(ids) != sorted(q["id"] for q in queue) or len(ids) != len(set(ids)):
            out({"id": rid, "error": {"code": -32600, "message": "queue reorder must include every queued submission exactly once"}})
        else:
            by = {q["id"]: q for q in queue}
            queue[:] = [by[i] for i in ids]
            out({"id": rid, "result": {}})
    else:
        out({"id": rid, "error": {"code": -32601, "message": "method not found: %s" % m}})
