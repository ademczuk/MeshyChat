"""Postgres gate: real signed write after the CLI's one-key auto-close decision.

Not part of DB-free validation. Run on the isolated migrated HQB gate database.
A mutant spending CALLS in views.owed_key must fail: the signed write cannot land.
"""
import uuid
from datetime import datetime, timezone

from django.test import override_settings
from apps.core import peer_limit
from apps.mesh import projection, graph, views
from tools.meshy_node import cli, node
from tests.test_call_routing import (client, owner_conn, world, server_config, bearer, signed, invoke)  # noqa:F401
from tests.test_mesh_graphread import _mesh_claimant, _grant, _row
from tests.test_meshy_node_owed import _state


def test_last_token_answer_is_stored_and_closes_real_question(client, owner_conn, world, monkeypatch, tmp_path, capsys):
    caller=world['caller']; peer=caller['peer_id']; ns='shared/mesh-chat'
    claimant=_mesh_claimant(owner_conn)
    _grant(owner_conn,ns,caller,claimant,permission='write')
    label=f'peer-{str(peer)[:8]}'
    thread='owed-key-'+uuid.uuid4().hex[:12]
    key=_row(owner_conn,claimant | {'label':'asker'},
             {'to':label,'thread_id':thread,'requires_ack':True,'type':'question','body':'private'})['key']
    owner_conn.commit();projection.rebuild(owner_conn);owner_conn.commit()
    path=_state(tmp_path,'http://testserver')
    monkeypatch.setattr(node,'ledger_records',lambda *a:[{'key':key,'version':1}])
    routes=[]
    def get(path,state,route,query,**kw):
        routes.append(route)
        response=client.get(route,query,headers=bearer(caller))
        if response.status_code!=200: raise node.Refused('routing check refused')
        return response.json()
    monkeypatch.setattr(node,'_get',get)
    # Broad post-send hint is separate, after the stored write; do not spend another token for it here.
    monkeypatch.setattr(node,'owed',lambda *a,**kw:{'owed':[]})
    landed=[]
    def write(path,state,**kw):
        args=kw['args']
        # node.send has already built the message; bind it to this test's actual authenticated peer.
        args=dict(args,value=dict(args['value'], **{'from':label}))
        args['key']=f"msg/{int(datetime.now(timezone.utc).timestamp()*1000)}-{label}-{uuid.uuid4().hex[:6]}"
        env=signed(caller,world['memory_peer'],'memory.write',args)
        response=invoke(client,caller,world['memory_peer'],'memory.write',env)
        assert response.status_code==202,response.content
        assert response.json()['status']=='completed',response.content
        landed.append(args['key'])
        return {'status':'completed','result':{'key':args['key']},'result_signature_verified':True}
    monkeypatch.setattr(node,'invoke',write)
    monkeypatch.setattr(views,'_owed_key_seen',{})
    monkeypatch.setattr(peer_limit,'_limiter',None)
    monkeypatch.setattr(peer_limit.time,'monotonic',lambda:1000.0)
    charges=[];real_admit=peer_limit.admit
    def admit(pid,**kw):
        charges.append((str(pid),kw.get('transport')))
        return real_admit(pid,**kw)
    monkeypatch.setattr(peer_limit,'admit',admit)
    with override_settings(MESHY_PEER_RATE_BURST=1,MESHY_PEER_RATE_PER_SEC=1):
        assert cli.main(['send','--state-file',str(path),'--base-url','http://testserver',
                         '--to','asker','--thread',thread,'--type','answer','--in-reply-to',key,
                         '--body','resolved','--no-recipient-check'])==0
        assert len(landed)==1
        assert charges==[(str(peer),'http')],charges
        assert peer_limit._current()._buckets['calls:'+str(peer)][0]==0.0
    row=owner_conn.execute('SELECT value FROM shared_memory WHERE namespace=%s AND key=%s AND is_head',
                            (ns,landed[0])).fetchone()
    assert row[0]['closes']==[key]
    owner_conn.commit();projection.rebuild(owner_conn);owner_conn.commit()
    result=graph.owed_acks(owner_conn,[label],keys=[key],states=['closed'])
    assert result and result[0].state=='closed'
    assert routes==['/v1/mesh/owed/key']
    capsys.readouterr()
