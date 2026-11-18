"""DB-free route/client contract and last-CALLS-token regression.

The real URLconf, wrapper, limiter and client send path run. Database/auth/projection and
write persistence are explicit stand-ins; test_mesh_owed_key_db.py covers the DB boundary.
"""
import contextlib
import json
import os
import types
from urllib.parse import urlencode

import pytest

# Set Django up here, as test_mesh_authorship.py does, so this file also runs alone and not only
# after a module that already did (kxxx's review of #407: 13 failed on "settings are not configured").
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
import django  # noqa: E402

django.setup()

from django.test import Client, override_settings  # noqa: E402
from apps.core import peer_limit  # noqa: E402
from apps.mesh import views  # noqa: E402
from tools.meshy_node import cli, node  # noqa: E402
from tests.test_meshy_node_owed import _state  # noqa: E402

KEY = 'msg/1790000000000-heron-question'


@pytest.fixture
def route(monkeypatch):
    peer = '11111111-1111-4111-8111-111111111111'
    row = types.SimpleNamespace(hit=types.SimpleNamespace(key=KEY, version=2),
                                state='open', msg_type='question', thread_id='ops/right',
                                done_when='PRIVATE BODY MUST NOT APPEAR')
    queries = []
    monkeypatch.setattr(views.dbconn, 'serving_connection', lambda: contextlib.nullcontext(object()))
    monkeypatch.setattr(views.dbconn, 'request_server_internal', lambda c: contextlib.nullcontext(c))
    monkeypatch.setattr(views, 'authenticate_peer', lambda c, r: types.SimpleNamespace(peer_id=peer))
    monkeypatch.setattr(views, '_mesh_read_refusal', lambda c, p: None)
    monkeypatch.setattr(views, 'labels_for', lambda c, p: [{'label': 'otter', 'source': 'roster'}])
    monkeypatch.setattr(views.usage, 'record', lambda *a: None)
    def owed(c, labels, **kw):
        queries.append((labels, kw))
        return (row,)
    monkeypatch.setattr(views.graph, 'owed_acks', owed)
    monkeypatch.setattr(views, '_owed_key_seen', {})
    monkeypatch.setattr(peer_limit.time, 'monotonic', lambda: 1000.0)
    monkeypatch.setattr(peer_limit, '_limiter', None)
    with override_settings(ROOT_URLCONF='meshychat.urls', ALLOWED_HOSTS=['testserver'],
                           MESHY_PEER_RATE_BURST=1, MESHY_PEER_RATE_PER_SEC=1):
        yield Client(), peer, queries, row


def test_single_key_route_has_no_content_and_preserves_one_token(route):
    client, peer, queries, row = route
    response = client.get('/v1/mesh/owed/key', {'key': KEY})
    assert response.status_code == 200
    assert response.json() == {'key': KEY, 'owed': [{'hit': {'key': KEY, 'version': 2},
                              'state': 'open', 'msg_type': 'question', 'thread_id': 'ops/right'}]}
    assert queries == [(['otter'], {'keys': [KEY], 'limit': 1,
                                   'states': ['open', 'active', 'answered', 'closed']})]
    peer_limit.admit(peer, transport='http')  # one write still fits
    assert client.get('/v1/mesh/owed/key', {'key': KEY}).status_code == 429


@pytest.mark.parametrize('query', [[], [('key', KEY), ('key', KEY)],
    [('key', KEY), ('body', 'yes')], [('key', KEY), ('fields', 'body')],
    [('key', KEY), ('keys', KEY)], [('key', 'msg/')], [('key', 'x'*300)]])
def test_overwide_queries_refused_beside_valid_control(route, query):
    client, _, queries, _ = route
    assert client.get('/v1/mesh/owed/key', {'key': KEY}).status_code == 200
    queries.clear()
    assert client.get('/v1/mesh/owed/key?'+urlencode(query)).status_code == 422
    assert queries == []


@pytest.mark.parametrize('key', [KEY + '-sender+tag@host', 'msg/1790000000000-' + 'q' * 490])
def test_every_key_a_close_may_name_is_looked_up(route, key):
    """Codex on #407: the route accepts what validate_mesh_message_v1 accepts in `closes` (msg/, <= 512 characters,
    an epoch-ms ts), so a valid question with a long key or an unusual sender suffix is not refused here."""
    client, _, queries, _ = route
    assert len(key) <= 512
    assert client.get('/v1/mesh/owed/key', {'key': key}).status_code == 200
    assert queries[-1][1]['keys'] == [key]


def test_a_comma_is_one_literal_key_never_two(route):
    """A message key's tail has no character rule, so "a,b" is a key a close could name. It is looked up as that ONE
    literal key (owed_acks matches keys exactly), never split into two: the one-key guarantee holds."""
    client, _, queries, _ = route
    joined = KEY + ',' + KEY
    queries.clear()
    assert client.get('/v1/mesh/owed/key', {'key': joined}).status_code == 200
    assert [q[1]['keys'] for q in queries] == [[joined]]


@pytest.mark.parametrize('key', ['msg/1790000000000-' + 'q' * 495, 'msg/20261025-not-epoch', 'msg/', KEY + '\x00'])
def test_keys_a_close_could_not_name_are_refused(route, key):
    """CONTROL: past 512 characters, or with a ts segment that is not an in-band epoch-ms, the write path refuses
    the close too, so the route refuses the lookup and never queries."""
    client, _, queries, _ = route
    queries.clear()
    assert client.get('/v1/mesh/owed/key', {'key': key}).status_code == 422
    assert queries == []


def test_body_and_read_denial_fail_closed(route, monkeypatch):
    from django.http import JsonResponse
    client, _, queries, _ = route
    assert client.get('/v1/mesh/owed/key', {'key': KEY}).status_code == 200
    queries.clear()
    assert client.generic('GET', '/v1/mesh/owed/key?'+urlencode({'key': KEY}),
                          data=b'{"body":true}', content_type='application/json').status_code == 422
    monkeypatch.setattr(views, '_mesh_read_refusal', lambda c,p: JsonResponse({'error':'denied'},status=403))
    assert client.get('/v1/mesh/owed/key', {'key': KEY}).status_code == 403
    assert queries == []


def test_independent_advisory_bound(route, monkeypatch):
    client, _, _, _ = route
    monkeypatch.setattr(views, 'RECIPIENTS_PER_MINUTE', 2)
    assert client.get('/v1/mesh/owed/key', {'key': KEY}).status_code == 200
    assert client.get('/v1/mesh/owed/key', {'key': KEY}).status_code == 200
    assert client.get('/v1/mesh/owed/key', {'key': KEY}).status_code == 429


@pytest.mark.parametrize('thread,closes', [('ops/right', [KEY]), ('ops/wrong', None)])
def test_last_calls_token_send_and_cross_thread_hint(route, monkeypatch, tmp_path, capsys, thread, closes):
    """Mutant spends_calls=True on owed_key spends the write's last token and fails here."""
    client, peer, _, _ = route
    path = _state(tmp_path, 'http://localhost')
    seen, writes = [], []
    def get(path, state, route, query, **kw):
        seen.append((route,query))
        response = client.get(route, query)
        if response.status_code != 200: raise node.Refused('lookup failed')
        return response.json()
    monkeypatch.setattr(node, '_get', get)
    monkeypatch.setattr(node, 'ledger_records', lambda *a: [{'key':KEY,'version':2}])
    # Advisory warning/after-send broad history are unrelated; ensure no network leaves this harness.
    monkeypatch.setattr(node, 'owed', lambda *a, **kw: {'owed': []})
    def write(*a, **kw):
        peer_limit.admit(peer, transport='http')
        writes.append(kw['args']['value'])
        return {'status':'completed','result':{},'result_signature_verified':True}
    monkeypatch.setattr(node, 'invoke', write)
    assert cli.main(['send','--base-url','http://localhost','--state-file',str(path),
                     '--to','heron','--thread',thread,'--type','answer','--in-reply-to',KEY,
                     '--body','settled','--no-recipient-check']) == 0
    assert len(writes) == 1 and writes[0].get('closes') == closes
    assert seen == [('/v1/mesh/owed/key', {'key': KEY})]
    assert peer_limit._current()._buckets['calls:'+peer][0] == 0.0
    assert ('ops/right' in capsys.readouterr().err) == (thread == 'ops/wrong')


@pytest.mark.parametrize('answer', [{}, {'key':'msg/wrong','owed':[]},
    {'key':KEY,'owed':[{},{}]}, {'key':KEY,'owed':[{'hit':{'key':'msg/wrong'}}]}])
def test_client_rejects_malformed_reply_without_list_fallback(monkeypatch, answer):
    calls=[]
    monkeypatch.setattr(node,'_get',lambda *a,**kw:calls.append(a[2]) or answer)
    with pytest.raises(node.Refused): node.owed_key(None,{},key=KEY)
    assert calls == ['/v1/mesh/owed/key']


def test_unavailable_point_route_never_falls_back_or_closes(monkeypatch, tmp_path, capsys):
    path = _state(tmp_path, 'http://localhost')
    routes, writes = [], []
    def unavailable(path, state, route, query, **kw):
        routes.append(route)
        raise node.Refused('404: older server')
    monkeypatch.setattr(node, '_get', unavailable)
    monkeypatch.setattr(node, 'owed', lambda *a, **kw: {'owed': []})
    monkeypatch.setattr(node, 'invoke', lambda *a, **kw: writes.append(kw['args']['value']) or
                        {'status': 'completed', 'result': {}, 'result_signature_verified': True})
    assert cli.main(['send', '--base-url', 'http://localhost', '--state-file', str(path),
                     '--to', 'heron', '--thread', 'ops/right', '--type', 'answer', '--in-reply-to', KEY,
                     '--body', 'settled', '--no-recipient-check']) == 0
    assert routes == ['/v1/mesh/owed/key']
    assert len(writes) == 1 and 'closes' not in writes[0]
    capsys.readouterr()


@pytest.mark.parametrize('status,hinted', [(429, True), (503, False)])
def test_a_rate_limited_lookup_says_so_and_still_sends_without_closing(monkeypatch, tmp_path, capsys, status, hinted):
    """txxxx's INFO on #407: the 31st reply in a minute gets a 429 from the point route. The send still
    goes and nothing is closed, but stderr says why and names --closes. CONTROL: any other server
    error stays silent, as before."""
    path = _state(tmp_path, 'http://localhost')
    writes = []
    def limited(path, state, route, query, **kw):
        raise node.ServerSaid(status, {'error': 'E_RATE_LIMITED' if status == 429 else 'E_UNAVAILABLE'})
    monkeypatch.setattr(node, '_get', limited)
    monkeypatch.setattr(node, 'owed', lambda *a, **kw: {'owed': []})
    monkeypatch.setattr(node, 'invoke', lambda *a, **kw: writes.append(kw['args']['value']) or
                        {'status': 'completed', 'result': {}, 'result_signature_verified': True})
    assert cli.main(['send', '--base-url', 'http://localhost', '--state-file', str(path),
                     '--to', 'heron', '--thread', 'ops/right', '--type', 'answer', '--in-reply-to', KEY,
                     '--body', 'settled', '--no-recipient-check']) == 0
    assert len(writes) == 1 and 'closes' not in writes[0]
    err = capsys.readouterr().err
    assert ('the owed lookup was rate-limited' in err) == hinted, err
    assert (f'--closes {KEY}' in err) == hinted, err


def test_the_real_routes_advisory_429_reaches_the_hint(route, monkeypatch, tmp_path, capsys):
    """The same through the real route: its own advisory bound answers 429, and node._get's real rule (any
    non-200 is ServerSaid) carries that status to the hint. Nothing is closed and the send still goes."""
    client, peer, queries, _ = route
    monkeypatch.setattr(views, 'RECIPIENTS_PER_MINUTE', 1)
    assert client.get('/v1/mesh/owed/key', {'key': KEY}).status_code == 200    # the bound's one slot, used
    path = _state(tmp_path, 'http://localhost')
    writes, statuses = [], []
    def get(path, state, route, query, **kw):
        response = client.get(route, query)
        statuses.append(response.status_code)
        if response.status_code != 200:
            raise node.ServerSaid(response.status_code, response.json())
        return response.json()
    monkeypatch.setattr(node, '_get', get)
    monkeypatch.setattr(node, 'ledger_records', lambda *a: [{'key': KEY, 'version': 2}])
    monkeypatch.setattr(node, 'owed', lambda *a, **kw: {'owed': []})
    monkeypatch.setattr(node, 'invoke', lambda *a, **kw: writes.append(kw['args']['value']) or
                        {'status': 'completed', 'result': {}, 'result_signature_verified': True})
    assert cli.main(['send', '--base-url', 'http://localhost', '--state-file', str(path),
                     '--to', 'heron', '--thread', 'ops/right', '--type', 'answer', '--in-reply-to', KEY,
                     '--body', 'settled', '--no-recipient-check']) == 0
    assert statuses == [429]
    assert len(writes) == 1 and 'closes' not in writes[0]
    assert 'the owed lookup was rate-limited' in capsys.readouterr().err
