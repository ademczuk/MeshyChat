"""The search indexes' periodic reconcile against real migrated tables (#459).

Written by codex-pxx-cli for pxx's review of #459 (c0fed9): on the prune-only head 6 of 19 failed, on main all 19
passed. Real local PG tables, signed synthetic source rows, the product writers and indexes. Cases: a row committed
later than the overlap window is recovered; an index over its ceiling refills from older live rows after a delete;
a reconciled index matches a fresh load in ids, vectors, ranks and lexical scores; dropped documents leave
scores unchanged.
"""
import os, datetime as dt, uuid, json, hashlib, math
import numpy as np
import psycopg
import pytest
os.environ.setdefault('DJANGO_SETTINGS_MODULE','meshychat.settings')
import django
django.setup()
from apps.core import rls
from apps.meshsem import search, lexical, graphsearch, store, spaces
from tests.fixtures import meshsem_db as db
from tests.fixtures import signed_rows as sr
NS="test/pr459"
T=dt.datetime(2026,10,29,0,0,tzinfo=dt.timezone.utc)

@pytest.fixture
def world():
    with psycopg.connect(os.environ.get('MESHY_TEST_DSN', 'postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django'),autocommit=True) as conn:
        db.ensure_namespace(conn)
        p=db.peer(conn,'pr459')
        conn.execute("INSERT INTO shared_memory_namespace(namespace,owner_peer_id,owner_owner_id,claimant_plane,ephemeral,retention_policy) VALUES(%s,%s,%s,'dev',FALSE,'keep_all') ON CONFLICT DO NOTHING",(NS,p['peer_id'],p['owner_id']))
        yield conn,p,db.make_space('pr459')

def put(world,n,at):
    conn,p,space=world
    rid=uuid.uuid4();r={'id':rid,'key':f'wiki/{rid}'}
    sr.insert(conn,sr.row(p['peer_id'],p['owner_id'],NS,r['key'],{'body_md':f'synthetic row {n} unique{n}'},rid=rid,call_id=f'call-{rid}',is_head=True,author_label=p['label'],embed_policy='allowed'))
    v=np.random.default_rng(n).standard_normal(space.dims).astype(np.float32)
    v/=np.linalg.norm(v)
    sha=hashlib.sha256(str(n).encode()).hexdigest()
    with rls.projection_writer(conn),conn.cursor() as cur:
        store.upsert_chunk_vectors(cur,space,NS,[(r['id'],r['key'],1,k,sha,v,'synthetic',T) for k in (0,1)])
        store.upsert_lexical_in(cur,space,NS,[(r['id'],r['key'],1,f'common unique{n}',sha,T)])
        cur.execute('UPDATE mesh_embeddings SET embedded_at=%s WHERE space=%s AND src_id=%s',(at,space.name,r['id']))
        cur.execute('UPDATE mesh_lexical SET indexed_at=%s WHERE space=%s AND src_id=%s',(at,space.name,r['id']))
    return str(r['id']),v

def idx_for(kind,space):
    if kind=='vector': return search.VectorIndex(space,NS)
    if kind=='chunk': return graphsearch.ChunkIndex(space,NS)
    return lexical.LexicalIndex(space.name,NS)

def refresh(conn,idx,ceiling=100,stale=False):
    if stale: idx.loaded_mono=-1e12
    with rls.server_internal(conn): idx.refresh(conn,ceiling=ceiling)

def keys(kind,sids):
    return {f'{s}:{n}' for s in sids for n in (0,1)} if kind=='chunk' else set(sids)

def delete(world,sids):
    conn,p,space=world
    with rls.projection_writer(conn),conn.cursor() as cur:
        for table in ('mesh_embeddings','mesh_lexical'):
            cur.execute(f'DELETE FROM {table} WHERE space=%s AND src_id=ANY(%s::uuid[])',(space.name,sids))

@pytest.mark.parametrize('kind',['vector','chunk','lexical'])
def test_late_commit_is_recovered_by_periodic_refresh(world,kind):
    conn,p,space=world
    put(world,1,T)
    idx=idx_for(kind,space);refresh(conn,idx)
    put(world,2,T+dt.timedelta(seconds=240));refresh(conn,idx)
    late,_=put(world,3,T+dt.timedelta(seconds=120))
    # COMMITTED after watermark advanced, stamped 120 seconds behind it.
    refresh(conn,idx)
    assert not keys(kind,[late]) & set(idx.ids)
    snapshots=[]
    for _ in range(3):
        refresh(conn,idx,stale=True)
        snapshots.append({'description':idx.describe(100),'late_present':keys(kind,[late])<=set(idx.ids)})
    print('PROBE',json.dumps({'case':'late_commit','kind':kind,'periods':snapshots}))
    assert keys(kind,[late])<=set(idx.ids), 'live late-committed row remains absent after 3 periodic refreshes'

@pytest.mark.parametrize('kind',['vector','chunk','lexical'])
def test_ceiling_deletion_refills_from_older_live_rows(world,kind):
    conn,p,space=world
    rows=[put(world,n,T+dt.timedelta(seconds=n*120))[0] for n in range(1,6)]
    ceiling=6 if kind=='chunk' else 3
    idx=idx_for(kind,space);refresh(conn,idx,ceiling)
    before=idx.describe(ceiling)
    assert idx.over_ceiling
    delete(world,[rows[-1]])
    refresh(conn,idx,ceiling,stale=True)
    after=idx.describe(ceiling)
    for _ in range(2):refresh(conn,idx,ceiling,stale=True)
    fresh=idx_for(kind,space);refresh(conn,fresh,ceiling)
    print('PROBE',json.dumps({'case':'ceiling_deletion','kind':kind,'before':before,'after':after,'three_periods':idx.describe(ceiling),'fresh':fresh.describe(ceiling),'missing_count':len(set(fresh.ids)-set(idx.ids))}))
    assert set(idx.ids)==set(fresh.ids), 'pruned ceiling index never backfills still-live older rows'

@pytest.mark.parametrize('kind',['vector','chunk','lexical'])
@pytest.mark.parametrize('gone',[(0,),(2,),(4,),(0,1,2,3,4)])
def test_prune_matches_fresh_survivor_vectors_and_ranks(world,kind,gone):
    conn,p,space=world
    rows=[put(world,n,T+dt.timedelta(seconds=n*120)) for n in range(5)]
    idx=idx_for(kind,space);refresh(conn,idx)
    delete(world,[rows[n][0] for n in gone]);refresh(conn,idx,stale=True)
    fresh=idx_for(kind,space);refresh(conn,fresh)
    surviving=[r[0] for n,r in enumerate(rows) if n not in gone]
    assert set(idx.ids)==keys(kind,surviving)==set(fresh.ids)
    assert idx.pos=={s:i for i,s in enumerate(idx.ids)}
    if kind=='lexical':
        for q in ['common', 'unique0','common unique0 unseenword']:
            a,av=idx.rank(*lexical.terms(q),limit=100); b,bv=fresh.rank(*lexical.terms(q),limit=100)
            assert a==b; np.testing.assert_allclose(av,bv)
        if idx.n and 'unique'+str(gone[0]) in idx.vocab:
            idf=idx._build()[-1][idx.vocab['unique'+str(gone[0])]]
            assert idf==math.log(1+idx.n)+1
    else:
        for s in idx.ids:
            np.testing.assert_array_equal(idx.vector_of(s),fresh.vector_of(s))
            assert np.array_equal(idx.buf[idx.pos[s]],idx.vector_of(s))
        for n,(sid,v) in enumerate(rows):
            a,av=idx.rank(v);b,bv=fresh.rank(v)
            assert a==b;np.testing.assert_array_equal(av,bv)
            if n in gone:
                for k in keys(kind,[sid]): assert idx.vector_of(k) is None
    print('PROBE',json.dumps({'case':'correctness','kind':kind,'drop':gone,'remaining':idx.n,'matched_fresh':True}))

def test_lexical_vocabulary_retention(world):
    conn,p,space=world
    keep,_=put(world,100,T)
    idx=idx_for('lexical',space);refresh(conn,idx)
    for n in range(10):
        sid,_=put(world,200+n,T+dt.timedelta(seconds=120*(n+1)))
        refresh(conn,idx);delete(world,[sid]);refresh(conn,idx,stale=True)
    fresh=idx_for('lexical',space);refresh(conn,fresh)
    assert idx.n==fresh.n==1
    for q in ['common unique100 unique209','common absent']:
        a,av=idx.rank(*lexical.terms(q),limit=100);b,bv=fresh.rank(*lexical.terms(q),limit=100)
        assert a==b;np.testing.assert_allclose(av,bv)
    print('PROBE',json.dumps({'case':'vocab_retention','periods':10,'description':idx.describe(100),'fresh':fresh.describe(100),'scores_equal':True}))
