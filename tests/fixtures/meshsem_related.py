"""Shared scaffolding for the persisted related_to edge tests (divergence 032).

`World` is one test's embedding space, fake model hosts, calibration file and settings;
`Scene` writes rows dated a millisecond apart just after the walks' watermarks. See
tests/test_meshsem_related.py for why the related walk STARTS at the embed watermark
(`start_here`) and why no row is ever dated later than a few milliseconds ahead.

The reranker answers a scripted table (`rerank_table`) and -10 for everything else, so a
test decides exactly which pairs cross the threshold and no other test's row ever does.
"""
from __future__ import annotations

import datetime as _dt
import importlib
import json
import uuid

from django.test import override_settings
from psycopg.rows import dict_row

from apps.mesh import projection as mesh_projection
from apps.meshsem import embedjob, search, spaces
from tests.fixtures import meshsem_db as db
from tests.fixtures.meshsem_fake import FakeModelHost


def mod(name: str):
    """A module this feature adds, imported where it is used (red-proof per test)."""
    return importlib.import_module(f"apps.meshsem.{name}")


class World:
    """One test's space, fake hosts, calibration file and settings."""

    def __init__(self, tmp_path, monkeypatch, *, extra_settings=None):
        self.extra = dict(extra_settings or {})
        self.space = db.make_space("rel", raw=True)
        monkeypatch.setitem(spaces.SPACES, self.space.name, self.space)
        self.embed = FakeModelHost(dims=db.DIMS)
        self.rr = FakeModelHost(dims=db.DIMS)
        self.rr.rerank_default = -10.0
        self.hosts = [self.embed, self.rr]
        self.rerankers = [("rerank-a", self.rr)]
        self.gate = None
        self.cal_path = tmp_path / "related_calibration.json"
        self._ctx = None
        self.calibrate(threshold=1.0, k=3)

    def host(self, **attrs) -> FakeModelHost:
        h = FakeModelHost(dims=db.DIMS)
        h.rerank_default = -10.0
        for k, v in attrs.items():
            setattr(h, k, v)
        self.hosts.append(h)
        return h

    def calibrate(self, *, threshold=1.0, k=3, enabled=True, reverse_depth=10, host=None):
        rc = mod("relatedcal")
        h = host or self.rr
        refs = [h.score_documents(q, list(docs)) for q, docs in rc.CANARY_SETS]
        self.cal = rc.build(space=self.space.name, threshold=threshold, k=k,
                            canary_references=refs, rerank_model="bge-fake",
                            reverse_depth=reverse_depth, enabled=enabled)
        self.cal_path.write_text(json.dumps(self.cal), encoding="utf-8")
        self.declare()

    def declare(self, *, rerankers=None, gate="keep", cal_path=None):
        if rerankers is not None:
            self.rerankers = rerankers
        if gate != "keep":
            self.gate = gate
        if self._ctx is not None:
            self._ctx.disable()
        provs = [{"id": "embed-a", "kind": "embed", "base_url": self.embed.base_url,
                  "space": self.space.name}]
        for rid, h in self.rerankers:
            provs.append({"id": rid, "kind": "rerank", "base_url": h.base_url,
                          "model": "bge-fake"})
        self._ctx = override_settings(
            MESHY_SEMANTIC_SPACE=self.space.name, MESHY_MODEL_PROVIDERS=provs,
            MESHY_EMBED_FORBIDDEN_ROWS=self.gate,
            MESHY_RELATED_CALIBRATION=str(cal_path or self.cal_path), **self.extra)
        self._ctx.enable()
        search.forget()

    def close(self):
        if self._ctx is not None:
            self._ctx.disable()
        for h in self.hosts:
            h.close()
        search.forget()


# ------------------------------------------------------------------------ helpers

def prepare(conn):
    """The two walks the related job reads behind: mesh_edges and the space's vectors."""
    for _ in range(500):
        if not mesh_projection.catch_up(conn).more:
            break
    for _ in range(500):
        r = embedjob.catch_up(conn)
        if not r.more or r.rows_processed == 0:
            break


def state(owner, space) -> dict | None:
    with owner.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM mesh_related_projection WHERE space = %s", (space.name,))
        return cur.fetchone()


def start_here(owner, world, app) -> tuple:
    """Drain both walks, then start the related walk at the embed watermark."""
    prepare(app)
    rc = mod("relatedcal")
    cal = rc.load(world.cal_path)
    with owner.cursor() as cur:
        cur.execute("SELECT watermark_at, watermark_id FROM mesh_embeddings_projection "
                    "WHERE space = %s", (world.space.name,))
        wm = cur.fetchone()
        cur.execute("INSERT INTO mesh_related_projection (space, threshold_version, "
                    "forbidden_rows_open, watermark_at, watermark_id) "
                    "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (space) DO UPDATE SET "
                    "threshold_version = EXCLUDED.threshold_version, "
                    "forbidden_rows_open = EXCLUDED.forbidden_rows_open, "
                    "watermark_at = EXCLUDED.watermark_at, "
                    "watermark_id = EXCLUDED.watermark_id",
                    (world.space.name, cal.threshold_version, world.gate is True,
                     wm[0], wm[1]))
    return wm


class Scene:
    """Rows dated just after the watermark, a millisecond apart, each in its own thread
    unless told otherwise. `text[name]` is the exact body the job sends for scoring."""

    def __init__(self, owner, tag="rel"):
        self.owner = owner
        self.p = db.peer(owner, tag)
        self.base = _dt.datetime.now(_dt.timezone.utc)
        self.n = 0
        self.rows: dict[str, dict] = {}
        self.text: dict[str, str] = {}
        self.uid = uuid.uuid4().hex[:10]

    def add(self, name, *, thread=None, reply_to=None, policy="allowed", when=None,
            rid=None, key=None):
        self.n += 1
        body = f"zr{self.uid} {name} message body"
        value = {"body": body, "thread_id": thread or f"t-{self.uid}-{name}"}
        if reply_to:
            value["in_reply_to"] = self.rows[reply_to]["key"] if reply_to in self.rows \
                else reply_to
        when = when or self.base + _dt.timedelta(milliseconds=self.n)
        r = db.row(self.owner, self.p, value, embed_policy=policy, created_at=when,
                   rid=rid, key=key)
        r["created_at"] = when
        r["thread"] = value["thread_id"]
        self.rows[name] = r
        self.text[name] = body
        return r

    def id(self, name) -> str:
        return str(self.rows[name]["id"])


def pair(host, scene, src, dst, fwd, rev):
    """Script both directions: forward = src's text as the query, dst's as the document."""
    host.rerank_table[(scene.text[src], scene.text[dst])] = fwd
    host.rerank_table[(scene.text[dst], scene.text[src])] = rev


def relate(conn, **kw) -> list:
    related = mod("related")
    out = []
    for _ in range(100):
        r = related.catch_up(conn, **kw)
        out.append(r)
        if r.status not in ("ok", "relate_bound", "deadline") or not r.more:
            break
    return out


def edges(owner, space, src_ids=None) -> list[dict]:
    sql = "SELECT * FROM mesh_related_edges WHERE space = %s"
    params: list = [space.name]
    if src_ids is not None:
        sql += " AND src_id = ANY(%s::uuid[])"
        params.append([str(s) for s in src_ids])
    with owner.cursor(row_factory=dict_row) as cur:
        cur.execute(sql + " ORDER BY src_id, rank", params)
        return cur.fetchall()


def runs(owner, space, src_ids) -> list[dict]:
    with owner.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM mesh_related_runs WHERE space = %s "
                    "AND src_id = ANY(%s::uuid[])", (space.name, [str(s) for s in src_ids]))
        return cur.fetchall()


def pairs(rows) -> set:
    return {(str(r["src_id"]), str(r["dst_id"])) for r in rows}
